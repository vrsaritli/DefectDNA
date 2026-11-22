"""RCA taxonomy contract shared by the gateway (owner) and the ai-knowledge-service.

The gateway stores versioned taxonomies per project and passes the current version
inside each ``analyze_defects`` job payload, so the ai-knowledge-service never reads
the gateway's collections.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

OTHER_CODE = "OTHER"
TaxonomyKind = Literal["root_causes", "sdlc_phases", "testing_gaps"]
TAXONOMY_KINDS: tuple[TaxonomyKind, ...] = ("root_causes", "sdlc_phases", "testing_gaps")


class TaxonomyItem(BaseModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,40}$")
    label: str = Field(min_length=1, max_length=80)
    definition: str = Field(default="", max_length=500)

    @field_validator("label", "definition")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class TaxonomyLists(BaseModel):
    root_causes: list[TaxonomyItem] = Field(min_length=1, max_length=50)
    sdlc_phases: list[TaxonomyItem] = Field(min_length=1, max_length=50)
    testing_gaps: list[TaxonomyItem] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_codes(self) -> "TaxonomyLists":
        for kind in TAXONOMY_KINDS:
            codes = [item.code for item in getattr(self, kind)]
            if len(codes) != len(set(codes)):
                raise ValueError(f"{kind} contains duplicate codes")
        return self


class Taxonomy(TaxonomyLists):
    version: int = Field(ge=1)

    def items(self, kind: TaxonomyKind) -> list[TaxonomyItem]:
        items: list[TaxonomyItem] = getattr(self, kind)
        return items

    def codes(self, kind: TaxonomyKind) -> list[str]:
        return [item.code for item in self.items(kind)]

    def label(self, kind: TaxonomyKind, code: str | None) -> str | None:
        for item in self.items(kind):
            if item.code == code:
                return item.label
        return None

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


STARTER_TAXONOMY: dict[str, Any] = {
    "version": 1,
    # Root causes follow the QA team's existing RCA categories (Architecture P8), taken
    # from the "Root Cause (RCA)" column of the sample ADO exports, plus Design,
    # Deployment and Other.
    "root_causes": [
        {
            "code": "REQUIREMENTS",
            "label": "Requirements",
            "definition": "The behaviour was never specified, or the requirement or acceptance "
            "criteria were incomplete, ambiguous or misunderstood; the build did what the "
            "story asked.",
        },
        {
            "code": "DESIGN",
            "label": "Design",
            "definition": "The architecture, data model, flow or UX design cannot satisfy "
            "the requirement, even when coded as designed.",
        },
        {
            "code": "CODE",
            "label": "Code",
            "definition": "Logic mistake in the implementation: wrong condition, calculation, "
            "parameter binding, null handling, off-by-one, missing branch.",
        },
        {
            "code": "INTEGRATION",
            "label": "Integration",
            "definition": "Our system and another service or team disagree on a contract, "
            "schema, event name, status code or shared value.",
        },
        {
            "code": "DATA",
            "label": "Data",
            "definition": "Bad, missing or unexpected data, reference data not loaded, or a "
            "faulty data fix or seed.",
        },
        {
            "code": "CONFIGURATION",
            "label": "Configuration",
            "definition": "Environment configuration, feature flag or setting differs from "
            "what is required; no code change needed.",
        },
        {
            "code": "DEPLOYMENT",
            "label": "Deployment",
            "definition": "The release process itself went wrong: migration not run, step "
            "missed, wrong artefact or pipeline setting.",
        },
        {
            "code": "THIRD_PARTY",
            "label": "Third Party",
            "definition": "Defect originates in an external library, SDK, vendor service or "
            "platform behaviour change.",
        },
        {
            "code": "SECURITY",
            "label": "Security",
            "definition": "A security control is missing or weak: data exposure, "
            "authentication, authorization or session handling.",
        },
        {
            "code": "TEST_GAP",
            "label": "Test Gap",
            "definition": "The scenario had no regression or functional test, so the "
            "defect escaped mainly because the path was never exercised before release.",
        },
        {
            "code": OTHER_CODE,
            "label": "Other",
            "definition": "None of the listed root causes fits.",
        },
    ],
    "sdlc_phases": [
        {
            "code": "REQUIREMENTS",
            "label": "Requirements",
            "definition": "Introduced while gathering or writing requirements.",
        },
        {
            "code": "DESIGN",
            "label": "Design",
            "definition": "Introduced in architecture, UX or technical design.",
        },
        {
            "code": "DEVELOPMENT",
            "label": "Development",
            "definition": "Introduced while writing or changing code.",
        },
        {
            "code": "INTEGRATION",
            "label": "Integration",
            "definition": "Introduced when combining components, services or vendors.",
        },
        {
            "code": "TESTING",
            "label": "Testing",
            "definition": "Introduced by test assets themselves, e.g. a wrong expected result "
            "that approved bad behaviour.",
        },
        {
            "code": "DEPLOYMENT",
            "label": "Deployment",
            "definition": "Introduced by release, configuration or infrastructure changes.",
        },
        {
            "code": "OPERATIONS",
            "label": "Operations",
            "definition": "Introduced in production operation: data fixes, manual changes, "
            "capacity drift.",
        },
        {
            "code": OTHER_CODE,
            "label": "Other",
            "definition": "None of the listed phases fits.",
        },
    ],
    "testing_gaps": [
        {
            "code": "NO_TEST_CASE",
            "label": "No test case",
            "definition": "No test existed for this scenario at all.",
        },
        {
            "code": "INSUFFICIENT_COVERAGE",
            "label": "Insufficient coverage",
            "definition": "Tests existed for the feature but missed this edge, negative or "
            "boundary case.",
        },
        {
            "code": "WRONG_EXPECTED_RESULT",
            "label": "Wrong expected result",
            "definition": "A test covered the scenario but its expected result was wrong.",
        },
        {
            "code": "TEST_DATA_GAP",
            "label": "Test data gap",
            "definition": "Tests ran, but the test data did not represent the failing data.",
        },
        {
            "code": "ENVIRONMENT_GAP",
            "label": "Environment gap",
            "definition": "The test environment differed from production (config, scale, "
            "integrations, browsers or devices).",
        },
        {
            "code": "NOT_EXECUTED",
            "label": "Test not executed",
            "definition": "A suitable test existed but was skipped, descoped or not run on "
            "this build.",
        },
        {
            "code": "REGRESSION_GAP",
            "label": "Regression gap",
            "definition": "A change broke previously working behaviour that the regression "
            "suite did not cover.",
        },
        {
            "code": "NON_FUNCTIONAL_GAP",
            "label": "Non-functional testing gap",
            "definition": "Performance, security, accessibility or reliability testing was "
            "missing or too shallow.",
        },
        {
            "code": OTHER_CODE,
            "label": "Other",
            "definition": "None of the listed testing gaps fits.",
        },
    ],
}


def starter_taxonomy() -> Taxonomy:
    return Taxonomy.model_validate(STARTER_TAXONOMY)
