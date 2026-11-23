"""Render manual regions and an itemized PDF without importing the AI engine.

Depends on Pillow, ReportLab and editor DTOs; native outputs share the exact
original dimensions and the PDF uses the same saved financial values as JSON.
"""
from io import BytesIO
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from api.private_demo.editor_schemas import CATEGORY_LABELS
from api.private_demo.catalog import DAMAGE_CLASSES
from api.private_demo.annotation_render import render_annotation

LABELS = CATEGORY_LABELS
BUSINESS_LABELS = {
    'expert': 'Expert', 'nom_assure': 'Assuré', 'compagnie': 'Compagnie', 'numero_police': 'N° de police',
    'intermediaire': 'Intermédiaire', 'garantie': 'Garantie', 'point_choc': 'Point de choc',
    'vin': 'V.I.N.', 'couleur': 'Couleur', 'carburant': 'Carburant', 'kilometrage': 'Kilométrage',
    'lieu_expertise': 'Lieu d’expertise',
}
INTERNAL_FIELDS = {'dossier_id', 'type', 'source', 'workflow_status', 'created_at', 'updated_at',
                   'creation_key', 'creation_request', 'before_finalized_at', 'evidence_finalized_at',
                   'imported_capture_resume', 'demo', 'status'}
DAMAGE_LABELS = dict(DAMAGE_CLASSES)

def money(value):
    return 'Non renseigné' if value is None else f'{value:,.2f} MAD'.replace(',', ' ')


def render_manual_report(document, result):
    output = BytesIO()
    styles = getSampleStyleSheet()
    paragraph = lambda value: Paragraph(escape(str(value)), styles['BodyText'])
    story = [Paragraph('CABEK AI — DÉMONSTRATION', styles['Title']), paragraph(document.title), Spacer(1, 12)]
    vehicle = result['vehicle']
    for label, value in [('Référence', result['dossier_id']), ('Véhicule', f'{vehicle["marque"]} {vehicle["modele"]}'),
                         ('Immatriculation', vehicle['matricule']), ('Mise en circulation', vehicle.get('date_mise_en_circulation')),
                         ('Date du sinistre', vehicle.get('date_sinistre'))]:
        story.append(paragraph(f'{label} : {value or "Non renseigné"}'))
    financial = result['financial']
    story += [Spacer(1, 14), Paragraph('Estimation manuelle', styles['Heading2'])]
    rows = [['Poste', 'Montant', 'Heures']]
    for key, label in LABELS.items():
        duration = financial['labor_hours'].get(key)
        rows.append([label, money(financial['breakdown'][key]), '' if key not in financial['labor_hours'] else ('Non renseigné' if duration is None else f'{duration:g} h')])
    rows.append(['Total' if financial['status'] == 'complete' else 'Sous-total connu', money(financial['known_subtotal']), ''])
    table = Table(rows, colWidths=[230, 150, 110], repeatRows=1)
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eeeeee')),
                              ('GRID', (0, 0), (-1, -1), .5, colors.lightgrey),
                              ('FONTSIZE', (0, 0), (-1, -1), 9), ('BOTTOMPADDING', (0, 0), (-1, -1), 9)]))
    story.append(table)
    for warning in financial['warnings']:
        story.append(paragraph(warning))
    for part in result['parts']:
        story += [Spacer(1, 12), Paragraph(escape(part['name']), styles['Heading2']),
                  paragraph(('Remplacer' if part['action'] == 'replace' else 'Réparer') + ' — ' + money(part['financial']['estimated_total'])),
                  paragraph('Photos : ' + ', '.join(next((photo.original_filename or photo.id for photo in document.photos if photo.id == reference), reference) for reference in part['source_images']))]
        for key, label in LABELS.items():
            story.append(paragraph(f'{label} : {money(part["financial"]["breakdown"][key])}'))
        for damage in result['detections']:
            if damage['detection_id'] in part['detection_ids']:
                severity = {'low': 'Légère', 'medium': 'Modérée', 'high': 'Importante'}.get(damage['niveau'], damage['niveau'])
                story.append(paragraph(f'{DAMAGE_LABELS.get(damage["type"], damage["type"])} · {severity} · {damage.get("notes", "")}'))
    for key, value in document.metadata.items():
        if key not in vehicle and key not in INTERNAL_FIELDS and isinstance(value, (str, int, float)):
            story.append(paragraph(f'{BUSINESS_LABELS.get(key, key)} : {value}'))

    def footer(canvas, report):
        canvas.setFont('Helvetica', 8)
        canvas.drawString(40, 25, 'DÉMONSTRATION — Estimation provisoire à vérifier par un expert')
        canvas.drawRightString(A4[0] - 40, 25, str(report.page))

    SimpleDocTemplate(output, pagesize=A4, title=document.title, rightMargin=40, leftMargin=40).build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
