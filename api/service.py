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
    PhotoUrls,
    ScanState,
    Vehicle,
)


@dataclass(frozen=True)
class DossierListQuery:
    search: str = ""
    status: ScanState | None = None
    limit: int = 20
    offset: int = 0


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
                warnings.append(DossierWarning(
                    dossier_id=dossier_id,
                    code=error.code,
                    message="Dossier was skipped because its saved data is unavailable or invalid.",
                ))
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

    def read_result(self, dossier_id: str, root_path: str = "") -> DossierResult:
        dossier = self.repository.read_dossier(dossier_id)
        analysis = self.repository.read_analysis(dossier)
        state = self.repository.read_state(dossier, analysis)
        response = DossierResult(dossier_id=dossier_id, status=state)
        if state == "failed":
            response.error = ErrorDetail(code="scan_failed", message="The scan failed.")
        if analysis is None:
            return response
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

    def media_path(self, dossier_id: str, requested_path: str) -> Path:
        dossier = self.repository.read_dossier(dossier_id)
        return self.repository.media_path(dossier, requested_path)

    def report_path(self, dossier_id: str) -> Path:
        dossier = self.repository.read_dossier(dossier_id)
        if self.repository.read_analysis(dossier) is None:
            raise DossierError(404, "report_not_found", "Saved report is not available.")
        return confined_path(dossier.directory, "analysis/report.pdf")
