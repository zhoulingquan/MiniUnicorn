from miniunicorn.config.paths import get_embedding_model_dir
from miniunicorn.config.schema import AgentDefaults
from miniunicorn.embedding import MODEL_DIMENSION, MODEL_ID, MODEL_REVISION


def test_vector_recall_defaults_on_but_explicit_false_is_preserved():
    assert AgentDefaults().vector_recall is True
    assert AgentDefaults.model_validate({"vectorRecall": False}).vector_recall is False


def test_embedding_constants_and_path(monkeypatch, tmp_path):
    monkeypatch.setattr("miniunicorn.config.paths.get_data_dir", lambda: tmp_path)
    assert MODEL_ID == "BAAI/bge-small-zh-v1.5"
    assert MODEL_REVISION == "7999e1d3359715c523056ef9478215996d62a620"
    assert MODEL_DIMENSION == 512
    assert get_embedding_model_dir() == tmp_path / "models" / "bge-small-zh-v1.5" / MODEL_REVISION
