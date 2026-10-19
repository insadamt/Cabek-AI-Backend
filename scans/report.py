import io
from datetime import datetime
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from core.expertise_engine import calculer_detail_fourniture, detection_rejetee
from scans.report_format import _pdf_money, _pdf_text, _pdf_date, _pdf_num_to_words_fr

# ============================================================
# STANDALONE COPY OF THE EXISTING CABEK REPORT
# ============================================================
# Copying avoids importing the Streamlit page, which executes UI code at import time.

def generate_report(meta, instances, ag, financial):
    """Génère le rapport CABEK au format métier de référence (2 pages A4)."""
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=9 * mm,
        leftMargin=9 * mm,
        topMargin=25 * mm,
        bottomMargin=17 * mm,
        title="Rapport d'expertise CABEK",
        author="CABEK",
    )
    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        "CabekPdfNormal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.2,
        leading=8.5,
        textColor=colors.HexColor("#202020"),
    )
    small = ParagraphStyle(
        "CabekPdfSmall",
        parent=normal,
        fontSize=6.2,
        leading=7.2,
    )
    header_style = ParagraphStyle(
        "CabekPdfHeader",
        parent=normal,
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9,
        alignment=TA_CENTER,
    )
    section_style = ParagraphStyle(
        "CabekPdfSection",
        parent=normal,
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=10,
        spaceBefore=1,
        spaceAfter=1,
    )
    DARK = colors.HexColor("#222222")
    BORDER = colors.HexColor("#777777")
    LIGHT = colors.HexColor("#F2F2F2")
    WHITE = colors.white

    # ============================================================
    # EN-TÊTE / PIED DE PAGE — repris du document CABEK de référence.
    # ============================================================

    def header_footer(canvas, doc):
        canvas.saveState()
        width, height = A4
        x0, x1 = 9 * mm, width - 9 * mm
        canvas.setFont("Helvetica-Bold", 9)
        canvas.setFillColor(DARK)
        canvas.drawCentredString(width / 2, height - 10 * mm, "CABINET D’EXPERTISE KHAMLICHI")
        canvas.setFont("Helvetica", 6.5)
        canvas.drawCentredString(width / 2, height - 14 * mm, "Expert assermenté près les tribunaux du MAROC")
        canvas.drawCentredString(width / 2, height - 17.5 * mm,
                                 "Matériels roulant – Risque divers – Dégât des eaux – Marchandises transportées")
        canvas.setStrokeColor(BORDER)
        canvas.setLineWidth(0.35)
        canvas.line(x0, height - 20 * mm, x1, height - 20 * mm)
        canvas.setFont("Helvetica", 6.2)
        canvas.setFillColor(colors.HexColor("#333333"))
        canvas.drawCentredString(
            width / 2, 8.5 * mm,
            "49, Rue Karatchi 2ème étage N°7 / Tél : +212 522-458989 / +212 6 93887718"
        )
        canvas.drawCentredString(
            width / 2, 5.7 * mm,
            "E-mail : cabek.expertise@gmail.com / ICE : 001551053000058 - I.F: 1602830"
        )
        canvas.drawCentredString(
            width / 2, 2.9 * mm,
            "TAXE PROFESSIONNELLE : 37950415 - R.C : 152371 – C.N.S.S: 7270903"
        )
        canvas.restoreState()
    story = []
    story.append(Paragraph("ESTIMATION PROVISOIRE - validation experte requise", normal))
    story.append(Paragraph(f"Sous-total connu (MAD) : {_pdf_money(financial['known_subtotal'])}", normal))
    if financial["status"] == "incomplete":
        story.append(Paragraph("CHIFFRAGE INCOMPLET - montant final indisponible", normal))
        for warning in financial["warnings"]:
            story.append(Paragraph(escape(warning), small))

    # ============================================================
    # PAGE 1 — RAPPORT D'EXPERTISE
    # ============================================================

    story.append(Paragraph(
        "RAPPORT D'EXPERTISE (MODE: RAPIDE)",
        ParagraphStyle("ReportTitle", parent=normal, fontName="Helvetica-Bold", fontSize=10.5,
                       leading=12, alignment=TA_CENTER, spaceAfter=2),
    ))
    ref = _pdf_text(meta.get("reference") or meta.get("ref") or meta.get("dossier_id"))
    date_ouverture = _pdf_date(meta.get("date_ouverture") or meta.get("created_at"))
    date_sinistre = _pdf_date(meta.get("date_sinistre"))
    expert = _pdf_text(meta.get("expert"), "Cabek")
    compte = _pdf_text(meta.get("pour_le_compte") or meta.get("compagnie"), "----")
    sinistre = _pdf_text(meta.get("sinistre") or meta.get("sinistre_nom") or meta.get("nom_assure"))
    intermediaire = _pdf_text(meta.get("intermediaire"))
    police = _pdf_text(meta.get("numero_police") or meta.get("police"))
    garantie = _pdf_text(meta.get("garantie") or meta.get("mode"), "RAPIDE")
    matricule = _pdf_text(meta.get("matricule"))
    adverse = _pdf_text(meta.get("sinistre_adverse") or meta.get("sinistre_adverse_nom"))
    info_rows = [
        ["Ref :", ref, "Date d’ouverture", date_ouverture],
        ["Date du sinistre", date_sinistre, "Par l’Expert", expert],
        ["Pour le compte de", compte, "Le sinistré", sinistre],
        ["Par l’intermédiaire de", intermediaire, "N° de police", police],
        ["Garantie affectée", garantie, "Matricule", matricule],
        ["Le sinistré adverse", adverse, "", ""],
        ["Assuré par N° de police", _pdf_text(meta.get("police_adverse")), "Propriétaire d’une", _pdf_text(meta.get("proprietaire"))],
        ["Matricule adverse", _pdf_text(meta.get("matricule_adverse")), "", ""],
    ]
    info = Table(info_rows, colWidths=[38*mm, 48*mm, 38*mm, 52*mm], hAlign="LEFT")
    info.setStyle(TableStyle([
        ("GRID", (0,0), (-1,-1), 0.3, BORDER),
        ("BACKGROUND", (0,0), (0,-1), LIGHT),
        ("BACKGROUND", (2,0), (2,-1), LIGHT),
        ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
        ("FONTNAME", (2,0), (2,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 6.6),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 1.2),
        ("BOTTOMPADDING", (0,0), (-1,-1), 1.2),
        ("LEFTPADDING", (0,0), (-1,-1), 3),
        ("RIGHTPADDING", (0,0), (-1,-1), 3),
    ]))
    story.append(info)
    story.append(Spacer(1, 1.0*mm))
    dates_rows = [
        ["Expertise avant réparation le", _pdf_date(meta.get("date_expertise_avant")),
         "Réception du devis des réparations le", _pdf_date(meta.get("date_devis_recu"))],
        ["Devis des réparations accordé le", _pdf_date(meta.get("date_devis_accorde")),
         "Expertise en cours de réparation le", _pdf_date(meta.get("date_en_cours_reparation"))],
        ["Expertise après réparation le", _pdf_date(meta.get("date_apres_reparation")),
         "Réception de la facture des réparations le", _pdf_date(meta.get("date_facture_recue"))],
        ["Lieu d’expertise", _pdf_text(meta.get("lieu_expertise"), "Autre Garage"),
         "Montant", _pdf_money(financial["estimated_total"], dash_if_zero=True)],
    ]
    dt = Table(dates_rows, colWidths=[48*mm, 40*mm, 52*mm, 36*mm])
    dt.setStyle(TableStyle([
        ("GRID", (0,0), (-1,-1), 0.3, BORDER),
        ("BACKGROUND", (0,0), (0,-1), LIGHT),
        ("BACKGROUND", (2,0), (2,-1), LIGHT),
        ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
        ("FONTNAME", (2,0), (2,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 6.5),
        ("TOPPADDING", (0,0), (-1,-1), 1.2),
        ("BOTTOMPADDING", (0,0), (-1,-1), 1.2),
    ]))
    story.append(dt)
    story.append(Spacer(1, 1.0*mm))

    # ============================================================
    # ============================================================
    # Véhicule
    # ============================================================
    # ============================================================

    story.append(Paragraph("Véhicule expertisé", section_style))
    vehicle_rows = [
        ["Point de choc", _pdf_text(meta.get("point_choc")), "Marque", _pdf_text(meta.get("marque"))],
        ["Model", _pdf_text(meta.get("modele")), "Cylindre", _pdf_text(meta.get("cylindre"))],
        ["N° d'immatriculation", matricule, "Date de M.C.", _pdf_text(meta.get("date_mc") or meta.get("annee"))],
        ["Type Mine", _pdf_text(meta.get("type_mine")), "Couleur /Teinte", _pdf_text(meta.get("couleur"))],
        ["V.I.N", _pdf_text(meta.get("vin")), "Carburant", _pdf_text(meta.get("carburant"))],
        ["Puissance fiscale", _pdf_text(meta.get("puissance_fiscale")), "kilométrage", _pdf_text(meta.get("kilometrage"))],
        ["Dernière V.Technique", _pdf_text(meta.get("derniere_vt")), "Usure des pneus", _pdf_text(meta.get("usure_pneus"))],
        ["Etat général post-sinistre", _pdf_text(meta.get("etat_general"), "Bon"), "", ""],
    ]
    vt = Table(vehicle_rows, colWidths=[40*mm, 50*mm, 40*mm, 46*mm])
    vt.setStyle(TableStyle([
        ("GRID", (0,0), (-1,-1), 0.3, BORDER),
        ("BACKGROUND", (0,0), (0,-1), LIGHT),
        ("BACKGROUND", (2,0), (2,-1), LIGHT),
        ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
        ("FONTNAME", (2,0), (2,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 6.6),
        ("TOPPADDING", (0,0), (-1,-1), 1.2),
        ("BOTTOMPADDING", (0,0), (-1,-1), 1.2),
    ]))
    story.append(vt)
    story.append(Spacer(1, 1.0*mm))

    # ============================================================
    # ============================================================
    # Évaluation chiffrée
    # ============================================================
    # ============================================================

    story.append(Paragraph(
        "Evaluation chiffrée des dommages (Si appliqué, le taux de TVA est 20%)",
        section_style,
    ))
    total_rep = float(ag.get("total_mo_reparation", 0) or 0)
    total_peinture = float(ag.get("total_mo_peinture", 0) or 0)
    total_mot = float(ag.get("total_mot", 0) or 0)
    total_produit = float(ag.get("total_produit_peinture", 0) or 0)
    detail_fourniture = calculer_detail_fourniture(instances)
    total_fourniture = float(detail_fourniture.get("total_fourniture", ag.get("total_fourniture", 0)) or 0)
    mo_rows = [["Main d'oeuvre", "Taux horaires(Dhs/h)", "Temps imparti(h)", "Total H.T", "Total TVA", "Total T.T.C"]]
    mo_rows += [
        ["Dressage", "F", "F", _pdf_money(financial["breakdown"]["repair_labor"]), _pdf_money(0), _pdf_money(financial["breakdown"]["repair_labor"])],
        ["Changement", "F", "F", _pdf_money(financial["breakdown"]["replacement_labor"]), _pdf_money(0), _pdf_money(financial["breakdown"]["replacement_labor"])],
        ["Mecanique", "0", "0", _pdf_money(0), _pdf_money(0), _pdf_money(0)],
        ["Peinture", "F", "F", _pdf_money(financial["breakdown"]["paint_labor"]), _pdf_money(0), _pdf_money(financial["breakdown"]["paint_labor"])],
        ["Electrique", "0", "0", _pdf_money(0), _pdf_money(0), _pdf_money(0)],
        ["Pare Brise", "0", "0", _pdf_money(0), _pdf_money(0), _pdf_money(0)],
    ]
    total_mo = total_rep + total_mot + total_peinture
    mo_rows += [["Conclusion: Total M.O", "", "", _pdf_money(total_mo), _pdf_money(0), _pdf_money(total_mo)]]
    mo_rows += [["Total Fournitures", "", "", _pdf_money(financial["breakdown"]["parts_supply"]), _pdf_money(0), _pdf_money(financial["breakdown"]["parts_supply"])]]
    mo_rows += [["Montant des dommages", "", "", _pdf_money(financial["estimated_total"]), _pdf_money(0), _pdf_money(financial["estimated_total"])]]
    mot = Table(mo_rows, colWidths=[43*mm, 31*mm, 28*mm, 27*mm, 27*mm, 27*mm], repeatRows=1)
    mot.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), LIGHT),
        ("GRID", (0,0), (-1,-1), 0.3, BORDER),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTNAME", (0,-3), (-1,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 6.4),
        ("ALIGN", (1,1), (-1,-1), "RIGHT"),
        ("TOPPADDING", (0,0), (-1,-1), 1),
        ("BOTTOMPADDING", (0,0), (-1,-1), 1),
    ]))
    story.append(mot)
    story.append(Spacer(1, 1.0*mm))

    # ============================================================
    # ============================================================
    # Indemnisation
    # ============================================================
    # ============================================================

    montant_dommages = financial["known_subtotal"]
    plafond = meta.get("plafond")
    vetuste = float(meta.get("vetuste", 0) or 0)
    remise = float(meta.get("remise", 0) or 0)
    tva = float(meta.get("tva", 0) or 0)
    franchise = float(meta.get("franchise", 0) or 0)
    indemnisation = float(meta.get("montant_indemnisation", montant_dommages - vetuste - remise - tva - franchise) or 0)
    if financial["status"] == "incomplete":
        indemnisation = None
    indemn_rows = [
        ["", "Total H.T", "Total T.T.C"],
        ["Plafond", _pdf_text(plafond, "----"), _pdf_money(0)],
        ["-Vétusté", _pdf_money(vetuste), "----"],
        ["-Remise", _pdf_money(remise), "----"],
        ["-T.V.A (20%)", _pdf_money(tva), "----"],
        ["Franchise", _pdf_money(franchise), "----"],
        ["Montatnt d'indemnisation", _pdf_money(indemnisation), _pdf_money(indemnisation)],
    ]
    ind = Table(indemn_rows, colWidths=[82*mm, 52*mm, 49*mm])
    ind.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), LIGHT),
        ("GRID", (0,0), (-1,-1), 0.3, BORDER),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTNAME", (0,-1), (-1,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 6.5),
        ("ALIGN", (1,1), (-1,-1), "RIGHT"),
        ("TOPPADDING", (0,0), (-1,-1), 1.2),
        ("BOTTOMPADDING", (0,0), (-1,-1), 1.2),
    ]))
    story.append(ind)
    story.append(Spacer(1, 1.0*mm))
    story.append(Paragraph("LE PRESENT RAPPORT EST ARRÊTE A LA SOMME DE :", normal))
    story.append(Paragraph(f"En chiffre <b>{_pdf_money(indemnisation)} DH</b>", normal))
    story.append(Paragraph(f"En lettre <b>{_pdf_num_to_words_fr(indemnisation)} dirhams</b>", normal))
    story.append(Spacer(1, 0.8*mm))
    story.append(Paragraph(
        "En foi de quoi, nous avons dressé et clos le présent rapport d'expertise pour servir et valoir ce que de droit",
        normal,
    ))
    story.append(Spacer(1, 1.0*mm))
    story.append(Paragraph(
        f"M.M'hamed Khamlichi Cabek, {_pdf_date(meta.get('date_rapport') or meta.get('date_expertise_avant') or datetime.now().date().isoformat())}",
        normal,
    ))

    # ============================================================
    # PAGE 2 — DÉTAILS FOURNITURES & PEINTURE
    # ============================================================

    story.append(PageBreak())
    story.append(Paragraph(
        "Détails fournitures & ingrédients peinture (Si appliqué, le taux de TVA est 20%)",
        ParagraphStyle("DetailTitle", parent=normal, fontName="Helvetica-Bold", fontSize=8.5,
                       leading=10, spaceAfter=2),
    ))
    detail_rows = [[
        "Désignation", "Qté", "Opération", "Type pièce", "P.U H.T", "TVA U", "P.U T.T.C",
        "Vetusté %", "Remise %", "T.T.C"
    ]]

    # Le rapport final ne reprend que les détections non rejetées.

    for item in instances:
        if detection_rejetee(item) or item.get("chiffrage_bloque"):
            continue
        piece = _pdf_text(item.get("piece"), "Pièce")
        operation = "ECHANGE" if item.get("remplacement_requis") else "REPARATION"
        type_piece = _pdf_text(item.get("type_piece"))
        prix_piece = item.get("prix_piece")
        if operation == "ECHANGE":
            pu = float(prix_piece) if prix_piece is not None else None
        else:
            pu = 0.0
        detail_rows.append([
            piece.upper(),
            "1",
            operation,
            type_piece.upper(),
            _pdf_money(pu),
            _pdf_money(0),
            _pdf_money(pu),
            "--",
            "--",
            _pdf_money(pu),
        ])
    if total_produit > 0:
        detail_rows.append([
            "Peinture et ingredients", "1", "ECHANGE", "--",
            _pdf_money(financial["breakdown"]["paint_materials"]), _pdf_money(0), _pdf_money(financial["breakdown"]["paint_materials"]),
            "--", "--", _pdf_money(financial["breakdown"]["paint_materials"]),
        ])
    if len(detail_rows) == 1:
        detail_rows.append([
            "Aucune fourniture enregistrée", "", "", "", _pdf_money(0), _pdf_money(0),
            _pdf_money(0), "--", "--", _pdf_money(0),
        ])
    detail = Table(
        detail_rows,
        colWidths=[35*mm, 9*mm, 24*mm, 24*mm, 17*mm, 15*mm, 19*mm, 15*mm, 15*mm, 19*mm],
        repeatRows=1,
    )
    detail.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), LIGHT),
        ("GRID", (0,0), (-1,-1), 0.3, BORDER),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 5.8),
        ("ALIGN", (1,1), (-1,-1), "RIGHT"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 1),
        ("BOTTOMPADDING", (0,0), (-1,-1), 1),
        ("LEFTPADDING", (0,0), (-1,-1), 2),
        ("RIGHTPADDING", (0,0), (-1,-1), 2),
    ]))
    story.append(detail)
    document.build(
        story,
        onFirstPage=header_footer,
        onLaterPages=header_footer,
    )
    buffer.seek(0)
    return buffer.getvalue()
