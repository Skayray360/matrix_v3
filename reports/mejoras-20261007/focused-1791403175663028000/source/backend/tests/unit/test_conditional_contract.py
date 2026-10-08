# Creado por Aldo Garcia.
"""Contratos sinteticos: caso declarado, fecha de fuente, fila y condiciones."""

from dataclasses import replace
from fractions import Fraction

import pytest

from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.prompts import build_answer_messages
from app.common.errors import AnswerValidationError
from app.config import get_settings
from app.llm.model_policy import ModelPolicy
from app.llm.ollama_client import ChatResult
from app.rag.claim_context import claim_context, declared_case
from app.rag.grounding import GroundingReport, verify_grounding
from app.rag.numeric_grounding import numeric_claim_supported, tenure_tables
from app.rag.schemas import Evidence
from tests.response_samples import APPLICATION_LIMIT_ANSWER

TABLE = "Antigüedad %\n0 – 4.99 0\n5 – 5.99 41\n6 – 6.99 63\n7 – 7.99 81\n8 en adelante 92"
RULE = (
    "Portabilidad del Programa Flexible\n"
    "Los empleados que ingresaron antes del 1 de febrero de 2020 tienen derecho al 92% de la Aportación Base "
    "y para recibir la Aportación Complementaria y Aportación Variable, es de acuerdo a la tabla de antigüedad.\n"
    "Los empleados que ingresen a partir del 1 de febrero de 2020 tendrán derecho a la Aportación Base, "
    "Aportación Complementaria y Aportación Variable de acuerdo a la siguiente tabla de antigüedad:\n"
)
QUESTION = (
    "Según el documento de diciembre de 2041, si ingresé en mayo de 2020 y me retiro con "
    "6 años y 6 meses de antigüedad antes de jubilarme, ¿qué porcentaje corresponde?"
)
ANSWER = (
    "Si esos datos declarados son correctos, la Aportación Base, la Aportación Complementaria "
    "y la Aportación Variable se calculan al 63%."
)


def evidence(text=RULE + TABLE, **kwargs):
    data = {
        "source_id": "prestaciones/ejemplo#0",
        "text": text,
        "score": 0.9,
        "category": "prestaciones",
        "filename": "Programa Flexible diciembre 2041.pdf",
        "section": "pagina 1",
        "page_or_sheet": "pagina 1",
        "document_id": "synthetic-document",
        "chunk_id": "synthetic-chunk",
    }
    data.update(kwargs)
    return Evidence(**data)


def verify(answer, question=QUESTION, items=None):
    items = items or (evidence(),)
    return verify_grounding(answer, items, question=question, mode="cited", allow_general_knowledge=True)


def cite(answer, source="prestaciones/ejemplo#0"):
    return f"{answer} [[{source}]]"


def test_application_binds_declared_inputs_to_rule_and_row():
    result = claim_context(QUESTION, (evidence(),))
    assert result.case.tenure == Fraction(13, 2)
    assert len(result.applications) == 1, result.limitations
    assert result.applications[0].percent == 63
    assert len(result.applications[0].rule.table_subjects) == 3
    assert verify(cite(ANSWER)).grounded


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5.999", None),
        ("6", 63),
        ("6.001", 63),
        ("6.99", 63),
        ("6.991", None),
        ("7", 81),
        ("7.001", 81),
    ],
)
def test_exact_interval_boundaries_without_filling_gaps(value, expected):
    row = tenure_tables(TABLE)[0].select(Fraction(value))
    assert (row.percent if row else None) == expected


@pytest.mark.parametrize(
    ("entry", "fixed_count", "ok"),
    [
        ("31 de enero de 2020", 1, True),
        ("1 de febrero de 2020", 0, True),
        ("2 de febrero de 2020", 0, True),
        ("febrero de 2020", 0, True),
    ],
)
def test_entry_cutoff_before_at_and_after(entry, fixed_count, ok):
    result = claim_context(QUESTION.replace("mayo de 2020", entry), (evidence(),))
    assert bool(result.applications) == ok, result.limitations
    assert len(result.applications[0].rule.fixed) == fixed_count


def test_month_crossing_cutoff_is_ambiguous():
    result = claim_context(
        QUESTION.replace("mayo", "febrero"), (evidence((RULE + TABLE).replace("1 de febrero", "15 de febrero")),)
    )
    assert not result.applications
    assert "unica" in result.limitations[0][1]


def test_before_cutoff_does_not_apply_table_to_fixed_component():
    question = QUESTION.replace("mayo de 2020", "enero de 2020")
    assert (
        verify(cite(ANSWER), question).validation_detail == "porcentaje no corresponde a la fila y conceptos aplicables"
    )
    assert verify(
        cite("Si esos datos declarados son correctos, la Aportación Base se calcula al 92%."), question
    ).grounded
    assert verify(
        cite("Si esos datos declarados son correctos, la Aportación Complementaria se calcula al 63%."), question
    ).grounded


@pytest.mark.parametrize("percent", ["41", "81", "92", "64"])
def test_wrong_row_and_invented_percentage_are_rejected(percent):
    result = verify(cite(ANSWER.replace("63%", percent + "%")))
    assert not result.grounded
    assert "fila" in result.validation_detail


def test_explicit_wrong_interval_cannot_bypass_case_validation():
    assert not verify(cite("Para el tramo 7 – 7.99 la tabla indica 81%.")).grounded
    assert not verify(cite("Si los datos declarados son correctos, para el tramo 7 – 7.99 se calcula 81%.")).grounded
    assert not verify(cite("Si los datos declarados son correctos, para el tramo 7 – 7.99 se calcula 63%.")).grounded
    assert verify(cite("Si los datos declarados son correctos, para el tramo 6 – 6.99 se calcula 63%.")).grounded


def test_other_source_cannot_supply_percentage_for_wrong_row():
    assert not numeric_claim_supported(
        "Para el tramo 6 – 6.99 se aplica 81%.", (TABLE, "El valor es 6; el máximo es 6.99; la tasa es 81%.")
    )


def test_wrong_declared_date_and_incomplete_duration_stay_rejected():
    assert not verify(cite("Si los datos declarados son correctos, ingreso en abril de 2020; se calcula 63%.")).grounded
    assert declared_case(QUESTION.replace("6 años y 6 meses", "6 años 6 meses")).tenure is None


def test_notes_after_table_cannot_be_ignored():
    item = evidence(RULE + TABLE + "\nSalvo empleados con contrato temporal.")
    assert not claim_context(QUESTION, (item,)).applications
    assert not verify(cite(ANSWER), items=(item,)).grounded


def test_conditional_does_not_certify_personal_eligibility():
    assert (
        verify(cite("Si los datos declarados son correctos, recibirás 63%.")).reason
        == "elegibilidad personal no acreditada"
    )


def test_declared_duration_and_exact_derived_decimal_not_normative_values():
    assert verify(cite("Si los datos declarados son correctos, con 6 años y 6 meses se calcula 63%.")).grounded
    assert verify(cite("Si los datos declarados son correctos, con 6.5 años se calcula 63%.")).grounded
    assert verify(cite("Si los datos declarados son correctos, 6 + 6/12 = 6.5 años; se calcula 63%.")).grounded
    assert not verify(cite("Si los datos declarados son correctos, 6 + 6/12 = 7 años; se calcula 63%.")).grounded
    assert not verify(cite("Si los datos declarados son correctos, con 6.8 años se calcula 63%.")).grounded
    assert not verify(cite(ANSWER.replace("63%.", "63% y se pagan 900 pesos."))).grounded


def test_only_identified_declared_fields_are_accepted():
    answer = "Datos declarados: ingreso en mayo de 2020; antigüedad 6 años y 6 meses.\n" + cite(ANSWER)
    assert verify(answer).grounded
    assert not verify(answer.replace("6 años y 6 meses.", "6 años y 6 meses; bono 900 pesos.")).grounded
    assert not verify(cite("Usted tiene derecho al 63%.")).grounded
    assert declared_case("La regla concede 900 pesos por 6 años.").tenure is None


def test_metadata_year_is_scoped_to_same_cited_document():
    statement = "El documento de diciembre de 2041 describe la portabilidad."
    assert verify(cite(statement)).grounded
    assert not verify(cite(statement.replace("2041", "2042"))).grounded
    assert not verify(cite("Se pagan 2041 pesos.")).grounded
    other = evidence(source_id="otro#0", filename="Otro diciembre 2042.pdf")
    assert not verify(cite(statement.replace("2041", "2042")), items=(evidence(), other)).grounded


@pytest.mark.parametrize(
    "statement",
    [
        "Es el nombre actual del programa.",
        "La política está vigente.",
        "No cuento con información, pero el plan está vigente.",
    ],
)
def test_metadata_does_not_prove_current_validity(statement):
    assert verify(cite(statement)).reason == "vigencia actual no acreditada"


@pytest.mark.parametrize(
    "text",
    [
        "No cuento con información documental suficiente para indicar el importe; ¿cuál es el documento de referencia?",
        "¿Cuál es su condición sindical?",
        APPLICATION_LIMIT_ANSWER,
    ],
)
def test_safe_nonfactual_responses_do_not_need_fictional_citations(text):
    assert verify(text).grounded
    assert not verify(text + " Se pagan 900 pesos.").grounded


def test_table_row_relationship_without_user_case():
    assert numeric_claim_supported("Para el tramo 6 – 6.99 la tabla indica 63%.", (TABLE,))
    assert not numeric_claim_supported("Para el tramo 6 – 6.99 la tabla indica 81%.", (TABLE,))
    assert not numeric_claim_supported("Corresponde 63%.", (TABLE,))
    assert not numeric_claim_supported("Para el tramo 6 – 6.99 la tabla indica 92%.", (RULE + TABLE,))
    assert numeric_claim_supported("Para 8 en adelante la tabla indica 92%.", (TABLE,))
    assert not numeric_claim_supported("Para 5 en adelante la tabla indica 92%.", (TABLE,))
    assert not verify(cite("Si los datos declarados son correctos, para 5 en adelante se calcula 63%.")).grounded


@pytest.mark.parametrize(
    "source",
    [
        TABLE.replace("6 – 6.99 63", "6 – 6.99 63 999"),
        TABLE.replace("7 – 7.99", "6.5 – 7.99"),
        TABLE.replace("Antigüedad %", "Antigüedad"),
    ],
)
def test_bad_tables_do_not_provide_applications(source):
    assert not claim_context(QUESTION, (evidence(RULE + source),)).applications


@pytest.mark.parametrize("row", ["6 – 6.99 63%", "6 – 6.99 63% 999"])
def test_unparsed_table_cannot_supply_a_percentage_as_free_prose(row):
    source = TABLE.replace("6 – 6.99 63", row)
    assert not numeric_claim_supported("Se calcula 63%.", (source,))
    assert not numeric_claim_supported("Para el tramo 5 – 5.99 se calcula 63%.", (source,))


def test_extra_conditions_do_not_get_silently_dropped():
    item = evidence(RULE.replace("tendrán derecho", "siempre que aprueben un examen, tendrán derecho") + TABLE)
    assert not claim_context(QUESTION, (item,)).applications
    assert not verify(cite(ANSWER), items=(item,)).grounded


def test_heading_cannot_move_conditions_to_other_benefit():
    meal = evidence("Vales de alimentos\nEl personal temporal recibe 12%.", source_id="vales#0")
    bonus = evidence("Bono extraordinario\nEl personal permanente recibe 12%.", source_id="bono#0")
    assert (
        verify(
            cite("**Bono extraordinario:**\nEl personal temporal recibe 12%.", "vales#0"),
            question="Explica las prestaciones",
            items=(meal, bonus),
        ).reason
        == "beneficio y fuente citada no corresponden"
    )
    assert verify(
        cite("**Vales de alimentos:**\nEl personal temporal recibe 12%.", "vales#0"),
        question="Explica las prestaciones",
        items=(meal, bonus),
    ).grounded


def test_case_application_cannot_be_relabelled_general():
    result = verify("### Orientación general\nSi los datos declarados son correctos, corresponde 63%.")
    assert not result.grounded
    assert result.reason == "aplicacion documental en orientacion general"


def test_unknown_input_grammar_cannot_move_application_to_general():
    question = "Si ingresé en mayo de 2020 y tengo dieciocho meses, ¿qué porcentaje me corresponde?"
    assert not verify("### Orientación general\nCorresponde 92%.", question).grounded
    assert not verify(cite("Si los datos declarados son correctos, se calcula 92%."), question).grounded


def test_abstention_cannot_approve_an_invented_non_numeric_rule():
    text = "No cuento con información suficiente, pero todos los empleados reciben un vehículo."
    assert not verify(cite(text)).grounded


def test_real_agent_validates_application_and_retry_cause(monkeypatch):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")

    class Transport:
        def __init__(self):
            self.calls = []

        def chat(self, **kwargs):
            self.calls.append(kwargs)
            text = ANSWER.replace("63%", "81%") if len(self.calls) == 1 else ANSWER
            return ChatResult(content=text + " [[E1]]", model=kwargs["model"], latency_ms=1)

    transport = Transport()
    policy = ModelPolicy()
    result = KnowledgeAgent(llm=transport, policy=policy).synthesize(
        question=QUESTION,
        evidences=(evidence(),),
        model_name=policy.fast_model,
    )
    assert result.grounding.grounded
    assert result.regenerated
    assert len(transport.calls) == 2
    assert "fila y conceptos" in transport.calls[1]["messages"][1]["content"]
    assert "APLICACION_CONDICIONAL" in transport.calls[0]["messages"][1]["content"]
    assert "La orientacion de conocimiento general esta deshabilitada" in transport.calls[1]["messages"][0]["content"]


@pytest.mark.parametrize("has_table", [True, False])
def test_failed_generation_is_distinct_from_missing_application_evidence(monkeypatch, has_table):
    monkeypatch.setattr(get_settings(), "answer_evidence_mode", "cited")

    class Client:
        def chat(self, **kwargs):
            return ChatResult(content=ANSWER.replace("63%", "81%") + " [[E1]]",
                              model=kwargs["model"], latency_ms=1)

    policy = ModelPolicy()
    with pytest.raises(AnswerValidationError) as failed:
        KnowledgeAgent(llm=Client(), policy=policy).synthesize(
            question=QUESTION,
            evidences=(evidence() if has_table else evidence("El documento describe un programa."),),
            model_name=policy.fast_model,
        )
    expected = "porcentaje no corresponde" if has_table else "tabla o regla incompleta"
    assert expected in failed.value.detail



def test_retry_reports_actual_rejection_before_coverage():
    report = GroundingReport(grounded=False, reason="afirmacion documental final sin cita")
    note = KnowledgeAgent._retry_note(report, 0.0)
    assert report.reason in note
    assert "ignoro parte" not in note


def test_context_and_proof_only_use_present_evidence():
    messages = build_answer_messages(question=QUESTION, evidences=())
    assert "No se pudo comprobar una aplicacion" in messages[1]["content"]
    assert not verify(cite(ANSWER), items=(replace(evidence(), text="No hay tabla."),)).grounded


def test_real_packing_never_approves_application_when_table_does_not_fit(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "answer_evidence_mode", "cited")
    small = evidence("El documento describe un programa.", source_id="small#0")
    full = evidence()
    policy = ModelPolicy()
    # El presupuesto alcanza una fuente corta, pero no la unidad regla+tabla.
    size = sum(len(m["content"]) for m in build_answer_messages(question=QUESTION, evidences=(small,), documentary_only=True))
    monkeypatch.setattr(settings, "ollama_fast_num_ctx", 1400 + settings.ollama_fast_max_tokens + (size + 30) // 3)

    class Client:
        def chat(self, **kwargs):
            assert full.text not in kwargs["messages"][1]["content"]
            return ChatResult(content=ANSWER + " [[E1]]", model=kwargs["model"], latency_ms=1)

    from app.common.inference_errors import InferenceFailureError, InferenceFailureKind
    with pytest.raises(InferenceFailureError) as failed:
        KnowledgeAgent(llm=Client(), policy=policy).synthesize(
            question=QUESTION,
            evidences=(small, full),
            model_name=policy.fast_model,
        )
    assert failed.value.failure_kind is InferenceFailureKind.CONTEXT_LIMIT



def test_retry_keeps_original_alias_when_first_source_no_longer_fits(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "answer_evidence_mode", "cited")
    first = evidence("Contenido documental. " * 220, source_id="long#0")
    second = evidence("La tasa es 12%.", source_id="small#0", filename="small.pdf")
    question = "Según las fuentes, indica la tasa."
    size = sum(len(m["content"]) for m in build_answer_messages(question=question, evidences=(first, second), documentary_only=True))
    monkeypatch.setattr(settings, "ollama_fast_num_ctx", 1400 + settings.ollama_fast_max_tokens + (size + 4) // 3)

    class Client:
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                assert first.text in kwargs["messages"][1]["content"]
                text = "Dato no respaldado [[inexistente]]"
            else:
                assert first.text not in kwargs["messages"][1]["content"]
                assert "[cita: [[E2]]]" in kwargs["messages"][1]["content"]
                text = "La tasa es 12%. [[E2]]"
            return ChatResult(content=text, model=kwargs["model"], latency_ms=1)

    client = Client()
    policy = ModelPolicy()
    result = KnowledgeAgent(llm=client, policy=policy).synthesize(
        question=question,
        evidences=(first, second),
        model_name=policy.fast_model,
    )
    assert result.regenerated and result.grounding.grounded
    assert result.cited_source_ids == ("small#0",)



def test_context_keeps_self_contained_questions_and_ellipses():
    policy = ModelPolicy()
    for question in (QUESTION, "¿Qué es el programa Horizonte?", "Según el reglamento de becas de 2042, ¿qué cubre?"):
        assert policy.contextual_reference(question, prior_questions=("Explica nuestras prestaciones",)) == ""
        assert policy.contextual_reference(question, prior_questions=(question,)) == ""
    for question in ("¿Y cuánto?", "¿Qué requisitos tiene?", "¿Y con ocho años?"):
        assert policy.contextual_reference(question, prior_questions=("Explica nuestras prestaciones",))
    assert not policy.contextual_reference(
        "¿Qué es la fotosíntesis?", prior_questions=("Explica nuestras prestaciones",)
    )
