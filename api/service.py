from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from api.errors import DossierError
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
)


# ============================================================
# LIST QUERY PARAMETERS
# ============================================================

@dataclass(frozen=True)
class DossierListQuery:
    search: str = ""
    status: ScanState | None = None
    limit: int = 20
    offset: int = 0


# ============================================================
# DOSSIER RESPONSE ORCHESTRATION
# ============================================================

class DossierService:
    def __init__(self, repository: DossierRepository):
        self.repository = repository

    def list_dossiers(self, query: DossierListQuery) -> DossierList:
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
                analysis = self.repository.read_analysis(dossier)
                state = self.repository.read_state(dossier, analysis)
                summaries.append(DossierSummary(
                    dossier_id=dossier_id,
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

        # Count before the status filter and pagination so dashboard counters remain useful.

        counts = DossierCounts(all=len(summaries))
        for item in summaries:
            setattr(counts, item.status, getattr(counts, item.status) + 1)
        filtered = [item for item in summaries if query.status is None or item.status == query.status]
        return DossierList(
            items=filtered[query.offset:query.offset + query.limit],
            total=len(filtered), counts=counts, limit=query.limit,
            offset=query.offset, warnings=warnings,
        )

    def available_photo_count(self, dossier: DossierRecord) -> int:
        return sum(
            confined_path(dossier.directory, photo.original_path).is_file()
            for photo in dossier.photos.values()
        )

    # ============================================================
    # SAVED RESULT AND FRONTEND URLS
    # ============================================================

    def list_original_photos(self, dossier_id: str, root_path: str = "") -> list[OriginalPhoto]:
        dossier = self.repository.read_dossier(dossier_id)
        base_url = f"{root_path.rstrip('/')}/api/dossiers/{quote(dossier_id, safe='')}"

        # Originals remain available before analysis; never load or require generated artifacts.

        return [
            OriginalPhoto(
                photo_id=photo.photo_id,
                original_url=f"{base_url}/media/{quote(photo.original_path, safe='/')}",
            )
            for photo in dossier.photos.values()
            if confined_path(dossier.directory, photo.original_path).is_file()
        ]

    def read_result(self, dossier_id: str, root_path: str = "") -> DossierResult:
        dossier = self.repository.read_dossier(dossier_id)
        analysis = self.repository.read_analysis(dossier)
        state = self.repository.read_state(dossier, analysis)
        response = DossierResult(
            dossier_id=dossier_id, status=state, progress=self.repository.read_progress(dossier),
        )
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

        # Keep saved paths independent of deployment; include any reverse-proxy URL prefix here.

        base_url = f"{root_path.rstrip('/')}/api/dossiers/{quote(dossier_id, safe='')}"
        photos = [
            PhotoUrls(
                photo_id=photo.photo_id,
                **{
                    field.replace("_path", "_url"): f"{base_url}/media/{quote(path, safe='/')}"
                    for field, path in photo.model_dump().items()
                    if field != "photo_id"
                },
            )
            for photo in analysis.photos
        ]
        response.result = AnalysisResult(
            **analysis.model_dump(exclude={"dossier_id", "photos"}),
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
