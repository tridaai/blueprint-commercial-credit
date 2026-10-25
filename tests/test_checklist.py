from decimal import Decimal

from commercial_credit.checklist import generate_checklist
from commercial_credit.models import (
    DocumentKind,
    FacilityRequest,
    FacilityType,
)


def _facility(ft: FacilityType) -> FacilityRequest:
    return FacilityRequest(
        facility_type=ft, amount=Decimal("500000"), purpose="test", term_months=36
    )


def test_base_items_present_for_every_facility_type():
    for ft in FacilityType:
        kinds = {i.kind for i in generate_checklist(_facility(ft))}
        assert DocumentKind.FINANCIAL_STATEMENT in kinds
        assert DocumentKind.TAX_RETURN in kinds
        assert DocumentKind.DEBT_SCHEDULE in kinds
        assert DocumentKind.ORG_DOCUMENT in kinds


def test_three_years_of_financials_and_tax_returns_required():
    items = generate_checklist(_facility(FacilityType.TERM_LOAN))
    fin = [i for i in items if i.kind == DocumentKind.FINANCIAL_STATEMENT]
    assert any(i.periods_required == 3 for i in fin)
    tax = [i for i in items if i.kind == DocumentKind.TAX_RETURN]
    assert any(i.periods_required == 3 for i in tax)


def test_cre_checklist_requires_appraisal_rent_roll_environmental():
    kinds = {i.kind for i in generate_checklist(_facility(FacilityType.CRE_LOAN))}
    assert DocumentKind.APPRAISAL in kinds
    assert DocumentKind.RENT_ROLL in kinds
    assert DocumentKind.ENVIRONMENTAL_REPORT in kinds
    assert DocumentKind.LEASES in kinds


def test_revolver_checklist_requires_borrowing_base_pack():
    kinds = {i.kind for i in generate_checklist(_facility(FacilityType.REVOLVING_LINE))}
    assert DocumentKind.AR_AGING in kinds
    assert DocumentKind.INVENTORY_LISTING in kinds
    assert DocumentKind.BORROWING_BASE_CERTIFICATE in kinds


def test_construction_and_sba_specifics():
    construction = {i.kind for i in generate_checklist(_facility(FacilityType.CONSTRUCTION_LOAN))}
    assert DocumentKind.CONSTRUCTION_BUDGET in construction
    assert DocumentKind.PERMITS in construction
    sba = {i.kind for i in generate_checklist(_facility(FacilityType.SBA_7A))}
    assert DocumentKind.SBA_FORM in sba


def test_every_item_cites_its_requirement_source():
    for ft in FacilityType:
        for item in generate_checklist(_facility(ft)):
            assert item.requirement_source.startswith("policy:")
            assert item.required is True


def test_checklist_is_deterministic():
    a = generate_checklist(_facility(FacilityType.CRE_LOAN))
    b = generate_checklist(_facility(FacilityType.CRE_LOAN))
    assert [i.model_dump() for i in a] == [i.model_dump() for i in b]
    ids = [i.item_id for i in a]
    assert len(ids) == len(set(ids))
