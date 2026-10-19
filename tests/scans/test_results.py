import io
from unittest.mock import Mock

from pypdf import PdfReader
import pytest

pytest.importorskip("cv2")
pytest.importorskip("reportlab")

from scans.pricing import enrichir_prix_pieces
from scans.pricing_client import get_estimation
from scans.report import generate_report
from scans.results import build_financial, prepare_invoice_instances


def replacement(**overrides):
    return {
        "piece_ai_brut": "p_capot", "piece": "Capot", "remplacement_requis": True,
        "validation_expert": "a_valider", "cout_mot": 200, "prix_piece": None,
        "cout_detail": {}, **overrides,
    }


def test_known_replacement_labor_survives_missing_part_price():
    result = build_financial([replacement()])
    assert result.status == "incomplete"
    assert result.estimated_total is None
    assert result.known_subtotal == 200
    assert result.breakdown.parts_supply is None


def test_complete_finances_reconcile_all_five_categories():
    repair = {"cout_detail": {"cout_reparation": 100, "cout_mop": 80, "cout_met": 50}}
    result = build_financial([repair, replacement(prix_piece=500)])
    assert result.status == "complete"
    assert result.estimated_total == result.known_subtotal == 930


def test_partial_c2_warning_does_not_become_complete():
    result = build_financial([{
        "cout": 80, "cout_detail": {
            "cout_reparation": 0, "heures_reparation": None, "cout_mop": 80, "cout_met": 0,
            "avertissement": "Donnée(s) manquante(s) : heures MO Réparation, prix MET",
        },
    }])
    assert result.status == "incomplete"
    assert result.known_subtotal == 80
    assert result.breakdown.repair_labor is None
    assert result.breakdown.paint_materials is None


def test_duplicate_replacement_and_rejected_detections_are_not_billed_twice():
    original = [replacement(prix_piece=500), replacement(prix_piece=500),
                replacement(validation_expert="rejetee", prix_piece=500)]
    invoice = prepare_invoice_instances(original)
    result = build_financial(invoice)
    assert result.estimated_total == 700
    assert invoice[1]["chiffrage_bloque"] is True
    assert original[1].get("chiffrage_bloque") is None


def test_pricing_configuration_and_outages_leave_explicit_warnings(monkeypatch):
    monkeypatch.delenv("AUTOESTIMATE_BASE_URL", raising=False)
    monkeypatch.delenv("AUTOESTIMATE_TOKEN", raising=False)
    instances = enrichir_prix_pieces([replacement()], 1, 1, 2015, {})
    result = build_financial(instances)
    assert result.status == "incomplete"
    assert any("configured" in warning for warning in result.warnings)


def test_pricing_numeric_strings_are_normalized(monkeypatch):
    monkeypatch.setenv("AUTOESTIMATE_BASE_URL", "http://pricing.test/api/v1")
    monkeypatch.setenv("AUTOESTIMATE_TOKEN", "test-only")
    response = Mock()
    response.json.return_value = {"data": {"estimation": {"median": "500.50"}}}
    monkeypatch.setattr("scans.pricing_client.requests.get", Mock(return_value=response))
    assert get_estimation(1, 1, 1, "occasion")["median"] == 500.5


@pytest.mark.parametrize("complete", [False, True])
def test_copied_report_marks_provisional_and_matches_json_totals(complete):
    instances = [replacement(prix_piece=500 if complete else None)]
    financial = build_financial(instances)
    aggregates = {"total_mot": 200, "total_fourniture": 500 if complete else 0}
    report = generate_report({"dossier_id": "DOS-TEST"}, instances, aggregates, financial.model_dump())
    reader = PdfReader(io.BytesIO(report))
    text = " ".join(page.extract_text() for page in reader.pages)
    assert "ESTIMATION PROVISOIRE" in text
    assert "Sous-total connu" in text
    assert len(reader.pages) == 2
    if complete:
        assert "700,00" in text
    else:
        assert "CHIFFRAGE INCOMPLET" in text
        assert "INCOMPLET" in text
