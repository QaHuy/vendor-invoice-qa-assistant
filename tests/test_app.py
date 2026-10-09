from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from factories import make_pdf
from invoice_qa.domain.models import CandidateStatus
from invoice_qa.ui import helpers

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")
PAGES = ["Upload", "Review Queue", "Metrics", "About"]


def open_page(page: str) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=20).run()
    if page != "Upload":
        at.sidebar.radio[0].set_value(page).run()
    return at


def page_text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [i.value for i in at.info] + [w.value for w in at.warning]
    parts += [e.value for e in at.error] + [c.value for c in at.code]
    return "\n".join(str(p) for p in parts)


def all_rendered(at: AppTest) -> str:
    frames = "\n".join(frame.value.to_csv() for frame in at.dataframe)
    return page_text(at) + "\n" + frames


def test_app_loads_without_exception(db_path):
    at = AppTest.from_file(APP_PATH, default_timeout=20).run()
    assert not at.exception


def test_navigation_exposes_four_pages(db_path):
    at = AppTest.from_file(APP_PATH, default_timeout=20).run()
    assert at.sidebar.radio[0].options == PAGES


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_with_empty_database(db_path, page):
    at = open_page(page)
    assert not at.exception


def test_review_queue_empty_state_is_safe(db_path):
    at = open_page("Review Queue")
    assert "No candidates" in page_text(at)


def test_upload_control_renders_for_pdf_input(db_path):
    at = open_page("Upload")
    uploaders = at.get("file_uploader")
    assert len(uploaders) == 1
    assert uploaders[0].proto.type == [".pdf"]
    assert uploaders[0].proto.max_upload_size_mb == 5


def seed(intake, overrides=None, extra=None, name="invoice.pdf"):
    return intake.process_upload(name, make_pdf(overrides, extra)).candidate


def test_queue_summaries_mask_tax_ids(db_path, intake):
    seed(intake)
    at = open_page("Review Queue")
    rendered = all_rendered(at)
    assert "DEMO-TAX-1001" not in rendered
    assert "1001" in rendered


def test_detail_view_masks_tax_id_until_explicit_edit_action(db_path, intake):
    seed(intake)
    at = open_page("Review Queue")
    assert "DEMO-TAX-1001" not in all_rendered(at)
    assert not any(t.label == "Vendor Tax ID" for t in at.text_input)
    at.button(key="reveal_tax_id").click().run()
    assert any(t.label == "Vendor Tax ID" and t.value == "DEMO-TAX-1001" for t in at.text_input)
    fresh = open_page("Review Queue")
    assert not any(t.label == "Vendor Tax ID" for t in fresh.text_input)


def test_ui_errors_are_generic():
    for code in (
        "empty_file",
        "invalid_extension",
        "file_too_large",
        "unreadable_pdf",
        "encrypted_pdf",
        "no_pages",
        "invalid_pdf_header",
    ):
        assert helpers.upload_rejection_message(code) == helpers.GENERIC_UPLOAD_MESSAGE
    for code in ("extraction_failed", "no_extractable_text", "anything_else"):
        message = helpers.extraction_error_message(code)
        assert "retry" in message.lower()
        assert code not in message
    assert helpers.review_error_message("unknown_code") == helpers.GENERIC_REVIEW_MESSAGE


def test_terminal_candidate_actions_are_disabled(db_path, intake, review):
    candidate = seed(intake)
    review.approve(candidate.id, reviewer_label="Reviewer A")
    at = open_page("Review Queue")
    assert at.button(key="approve").disabled
    assert at.button(key="reject").disabled
    assert at.button(key="confirm_fields").disabled
    assert at.button(key="confirm_distinct").disabled
    assert at.button(key="save_corrections").disabled


def test_open_candidate_actions_are_enabled(db_path, intake):
    seed(intake)
    at = open_page("Review Queue")
    assert not at.button(key="approve").disabled
    assert not at.button(key="reject").disabled


def test_confirm_is_offered_only_for_confirmable_fields(db_path, intake):
    seed(
        intake,
        {"Vendor Name": None, "Subtotal": "1,0000"},
        ["Vendor_Name: Example Demo Loose Ltd."],
    )
    at = open_page("Review Queue")
    options = at.multiselect(key="confirm_field_names").options
    assert options == ["vendor_name"]


def test_blocking_only_candidate_offers_nothing_to_confirm(db_path, intake):
    seed(intake, {"Subtotal": "1,0000"})
    at = open_page("Review Queue")
    assert at.multiselect(key="confirm_field_names").options == []
    assert at.button(key="confirm_fields").disabled


def test_approval_through_the_ui_marks_candidate_approved(db_path, intake, database):
    candidate = seed(intake)
    at = open_page("Review Queue")
    at.text_input(key="reviewer_label").set_value("Reviewer A").run()
    at.button(key="approve").click().run()
    assert not at.exception
    assert database.get_candidate(candidate.id).status is CandidateStatus.APPROVED


def test_correction_error_is_shown_per_field(db_path, intake, database):
    candidate = seed(intake, {"Subtotal": "1,0000"})
    at = open_page("Review Queue")
    at.text_input(key="field_subtotal").set_value("1.000,00").run()
    at.button(key="save_corrections").click().run()
    assert any(e.value == "Subtotal: the value is malformed." for e in at.error)
    assert database.list_review_events(candidate.id) == []


def test_metrics_page_shows_the_evaluator_command_without_a_report(db_path, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    at = open_page("Metrics")
    assert any("scripts/evaluate.py" in code.value for code in at.code)


def test_about_states_synthetic_data_and_non_production_limits(db_path):
    text = page_text(open_page("About")).lower()
    assert "synthetic" in text
    assert "not production" in text or "not a production" in text
    assert "real-provider" in text or "real provider" in text
