from core.expertise_engine import detection_valide_pour_chiffrage, item_key


# ============================================================
# STANDALONE COPY OF EXISTING PRICE ENRICHMENT
# ============================================================

# A separate client lets workers use environment configuration without changing Streamlit.

def enrichir_prix_pieces(
    instances: list,
    marque_id: int | None,
    model_id: int | None,
    year: int | None,
    types_choisis: dict,
) -> list:
    """
    Complète le coût des dommages nécessitant un remplacement
    avec le prix pièce récupéré via AutoEstimate.

    Une détection rejetée est toujours ignorée.
    """

    if not marque_id or not model_id:
        for item in instances:
            if item.get("remplacement_requis") and detection_valide_pour_chiffrage(item):
                item.setdefault("cout_detail", {})["avertissement_api"] = "Vehicle pricing identifiers unavailable."
        return instances

    from scans.pricing_client import (
        get_estimation,
        AutoEstimateError,
    )

    from core.parts_mapping import (
        get_part_id,
    )

    for item in instances:

        # ============================================================
        # REJET EXPERT
        # ============================================================

        if not detection_valide_pour_chiffrage(item):
            continue

        # ============================================================
        # Seulement remplacement
        # ============================================================

        if not item.get(
            "remplacement_requis"
        ):
            continue

        key = item_key(item)

        type_piece = types_choisis.get(
            key,
            "occasion",
        )

        piece_ai = item.get(
            "piece_ai_brut",
            "",
        )

        part_id = get_part_id(
            piece_ai
        )

        detail = item.setdefault(
            "cout_detail",
            {},
        )

        # ============================================================
        # Pièce inconnue
        # ============================================================

        if part_id is None:

            detail[
                "avertissement_api"
            ] = (
                f"Pièce '{piece_ai}' non cartographiée "
                "vers AutoEstimate "
                "(voir core/parts_mapping.py)."
            )

            continue

        # ============================================================
        # API AutoEstimate
        # ============================================================

        try:

            est = get_estimation(
                marque_id,
                model_id,
                part_id,
                type_piece,
                year,
            )

        except AutoEstimateError as e:

            detail[
                "avertissement_api"
            ] = (
                f"AutoEstimate indisponible : {e}"
            )

            continue

        if est is None:

            detail[
                "avertissement_api"
            ] = (
                "Aucune observation AutoEstimate "
                f"pour ce type ({type_piece})."
            )

            continue

        # ============================================================
        # Prix médian
        # ============================================================

        prix_piece = est.get(
            "median"
        )

        item["prix_piece"] = prix_piece

        item["type_piece"] = type_piece

        item["estimation_detail"] = est

        cout_mot = item.get(
            "cout_mot"
        )

        if (
            cout_mot is not None
            and prix_piece is not None
        ):

            item["cout"] = round(
                cout_mot + prix_piece,
                2,
            )

            detail["prix_piece"] = (
                prix_piece
            )

            detail["type_piece"] = (
                type_piece
            )

            detail["total"] = (
                item["cout"]
            )

            detail.pop(
                "avertissement_api",
                None,
            )

    return instances
