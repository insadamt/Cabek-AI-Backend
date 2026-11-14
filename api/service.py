"""Assemble dossier lists, current metadata, comparisons, and saved scan results.

Depends on the file repository and pure workflow derivation; reads stay offline.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

from api.errors import DossierError
from api.labor import result_with_labor_hours
from api.files import confined_path
from api.repository import DossierRecord, DossierRepository
from api.schemas import (
    AnalysisResult,
    DossierCounts,
    DossierList,
    DossierResult,
    DossierSummary,
    DossierWarning,
    ErrorDetail,
    OriginalPhoto,
    PhotoUrls,
    ScanState,
    Vehicle,
    WorkflowPhoto,
)
from api.workflow_schemas import DossierSort, DossierType, WorkflowStatus
from api.workflow_state import available_photo_records, workflow_status, verification_comparison


# ============================================================
# LIST QUERY PARAMETERS
# ============================================================

@dataclass(frozen=True)
class DossierListQuery:
    search: str = ""
    status: ScanState | None = None
    limit: int = 20
    offset: int = 0
    type: DossierType | None = None
    workflow_status: WorkflowStatus | None = None
    sort: DossierSort = "id_desc"
    created_from: date | None = None
    created_to: date | None = None


# ============================================================
# DOSSIER RESPONSE ORCHESTRATION
# ============================================================

class DossierService:
    def __init__(self, repository: DossierRepository):
        self.repository = repository

    def list_dossiers(self, query: DossierListQuery) -> DossierList:
        if query.created_from and query.created_to and query.created_from > query.created_to:
            raise DossierError(422, "invalid_date_range", "created_from must be on or before created_to.")
        summaries = []
        warnings = []
        search = query.search.strip().casefold()
        for dossier_id in self.repository.dossier_ids():
            try:
                dossier = self.repository.read_dossier(dossier_id)
                vehicle = Vehicle.model_validate(dossier.metadata.model_dump())
                searchable = " ".join(
                    (dossier_id, vehicle.marque, vehicle.modele, vehicle.matricule)
                ).casefold()
                if search and search not in searchable:
                    continue
                created = parsed_timestamp(dossier.metadata.created_at)
                if query.created_from and (created is None or created.date() < query.created_from):
                    continue
                if query.created_to and (created is None or created.date() > query.created_to):
                    continue
                analysis, state = self.repository.read_analysis_state(dossier)
                summaries.append(DossierSummary(
                    dossier_id=dossier_id,
                    type=dossier.metadata.type, source=dossier.metadata.source,
                    workflow_status=workflow_status(dossier, state),
                    updated_at=dossier.metadata.updated_at,
                    vehicle=vehicle,
                    created_at=dossier.metadata.created_at,
                    status=state,
                    photo_count=self.available_photo_count(dossier),
                ))
            except DossierError as error:

                # One damaged folder should not prevent the dashboard from showing healthy dossiers.

                warnings.append(DossierWarning(
                    dossier_id=dossier_id,
                    code=error.code,
                    message="Dossier was skipped because its saved data is unavailable or invalid.",
                ))

        # Each counter ignores its own selector so category tabs and status chips remain useful.

        type_counts = {category: sum(item.type == category for item in summaries)
                       for category in ("forfait_expertise", "chiffrage_rapide", "verification")}
        summaries = [item for item in summaries if query.type is None or item.type == query.type]
        workflow_counts = {}
        for item in summaries:
            workflow_counts[item.workflow_status] = workflow_counts.get(item.workflow_status, 0) + 1
        summaries = [item for item in summaries if query.workflow_status is None or item.workflow_status == query.workflow_status]

        counts = DossierCounts(all=len(summaries))
        for item in summaries:
            setattr(counts, item.status, getattr(counts, item.status) + 1)
        filtered = [item for item in summaries if query.status is None or item.status == query.status]
        sort_field, direction = query.sort.rsplit("_", 1)
        filtered.sort(key=lambda item: (
            item.dossier_id if sort_field == "id" else
            parsed_timestamp(getattr(item, sort_field)) or datetime.min.replace(tzinfo=timezone.utc),
            item.dossier_id,
        ), reverse=direction == "desc")
        return DossierList(
            items=filtered[query.offset:query.offset + query.limit],
            total=len(filtered), counts=counts, limit=query.limit,
            offset=query.offset, warnings=warnings,
            type_counts=type_counts, workflow_counts=workflow_counts,
        )

    def available_photo_count(self, dossier: DossierRecord) -> int:
        return sum(
            confined_path(dossier.directory, photo.original_path).is_file()
            for photo in dossier.photos.values()
        )

    # ============================================================
    # SAVED RESULT AND FRONTEND URLS
    # ============================================================

    def original_photo_urls(self, dossier: DossierRecord, base_url: str) -> list[WorkflowPhoto | OriginalPhoto]:

        # Originals remain available before analysis; never load or require generated artifacts.

        records = {Path(photo.path.replace("\\", "/")).stem: photo for photo in dossier.metadata.photos}
        return [
            (WorkflowPhoto if records[photo.photo_id].side else OriginalPhoto)(
                photo_id=photo.photo_id,
                original_url=f"{base_url}/media/{quote(photo.original_path, safe='/')}",
                preview_url=f"{base_url}/preview/{quote(photo.original_path, safe='/')}.webp",
                **({"side": records[photo.photo_id].side, "phase": records[photo.photo_id].phase,
                    "uploaded_at": records[photo.photo_id].uploaded_at} if records[photo.photo_id].side else {}),
            )
            for photo in dossier.photos.values()
            if confined_path(dossier.directory, photo.original_path).is_file()
        ]

    def read_result(self, dossier_id: str, root_path: str = "") -> DossierResult:
        dossier = self.repository.read_dossier(dossier_id)
        base_url = f"{root_path.rstrip('/')}/api/dossiers/{quote(dossier_id, safe='')}"
        analysis, state = self.repository.read_analysis_state(dossier)
        response = DossierResult(
            dossier_id=dossier_id, status=state, progress=self.repository.read_progress(dossier),
            type=dossier.metadata.type, source=dossier.metadata.source,
            created_at=dossier.metadata.created_at, updated_at=dossier.metadata.updated_at,
            workflow_status=workflow_status(dossier, state),
            vehicle=Vehicle.model_validate(dossier.metadata.model_dump()),
            photos=self.original_photo_urls(dossier, base_url),
        )
        if dossier.metadata.type == "verification":
            available_ids = {Path(photo.path).stem for photo in available_photo_records(dossier)}
            response.comparison = verification_comparison([
                photo for photo in response.photos if photo.photo_id in available_ids
            ])
            response.comparison.before_locked = bool(dossier.metadata.before_finalized_at) or any(
                photo.phase == "after" for photo in dossier.metadata.photos
            )
            response.comparison.finalized = bool(dossier.metadata.evidence_finalized_at) or response.workflow_status == "comparison_ready"
        if state == "failed":

            # Stored worker errors may contain local paths or credentials; do not return them.

            response.error = ErrorDetail(code=self.repository.read_failure_code(dossier), message="The scan failed.")
        if analysis is None:
            return response
        if response.progress is not None:
            response.progress.stage = "completed"
            response.progress.annotations_ready = response.progress.annotations_expected
            response.progress.photos_completed = response.progress.photos_total
            response.progress.result_ready = response.progress.report_ready = True

        photos = [
            PhotoUrls(
                photo_id=photo.photo_id,
                **{
                    field.replace("_path", "_url"): f"{base_url}/media/{quote(path, safe='/')}"
                    for field, path in photo.model_dump().items()
                    if field != "photo_id"
                },
                **{
                    field.replace("_path", "_preview_url"): f"{base_url}/preview/{quote(path, safe='/')}.webp"
                    for field, path in photo.model_dump().items()
                    if field != "photo_id"
                },
            )
            for photo in analysis.photos
        ]
        response.result = AnalysisResult(
            **result_with_labor_hours(analysis),
            photos=photos, report_url=f"{base_url}/report",
        )
        return response

    # ============================================================
    # IMAGE AND REPORT ACCESS
    # ============================================================

    def media_path(self, dossier_id: str, requested_path: str) -> Path:
        dossier = self.repository.read_dossier(dossier_id)
        return self.repository.media_path(dossier, requested_path)

    def report_path(self, dossier_id: str) -> Path:
        dossier = self.repository.read_dossier(dossier_id)
        if self.repository.read_analysis(dossier) is None:
            raise DossierError(404, "report_not_found", "Saved report is not available.")
        return confined_path(dossier.directory, "analysis/report.pdf")


def parsed_timestamp(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
        if parsed is not None:
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    return None
