import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.ai.projects.models import FunctionTool, PromptAgentDefinition

from pii_rag.agent import INSTRUCTIONS, TOOL_PARAMETERS, FoundryAgent
from pii_rag.knowledge import Evidence
from pii_rag.pii import ProtectionError, SyntheticDetector


def test_foundry_prompt_agent_executes_scoped_iq_tool_and_keeps_protected_state():
    call = SimpleNamespace(
        type="function_call",
        name="retrieve_knowledge",
        call_id="call-1",
        arguments=json.dumps({"query": "Alice Martin saw HTTP 504"}),
    )
    api = Mock()
    api.responses.create.side_effect = [
        SimpleNamespace(status="completed", output=[call]),
        SimpleNamespace(
            status="completed", output=[], output_text="Reduce concurrency. [source:1]"
        ),
    ]
    project = Mock()
    project.get_openai_client.return_value = nullcontext(api)
    retriever = Mock()
    retriever.retrieve.return_value = [Evidence("ref", "Reduce concurrency.", "doc:v1", "support")]
    agent = FoundryAgent(project, retriever, SyntheticDetector())
    result = agent.answer("support", "protected-conversation", "What fixed HTTP 504?")
    assert result.answer == "Reduce concurrency. [source:1]"
    project.get_openai_client.assert_called_once_with(agent_name="pii-support-agent")
    assert retriever.retrieve.call_args.args == (
        "support",
        "[REDACTED_PERSON] saw HTTP 504",
    )
    requests = api.responses.create.call_args_list
    assert requests[0].kwargs["conversation"] == "protected-conversation"
    assert requests[0].kwargs["tool_choice"]["name"] == "retrieve_knowledge"
    tool_output = requests[1].kwargs["input"][0]
    evidence = json.loads(tool_output["output"])["evidence"]
    assert tool_output["call_id"] == "call-1"
    assert evidence[0]["id"] == "1"
    assert evidence[0]["content"] == "Reduce concurrency."


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("arbitrary_index_tool", '{"query":"checkout"}'),
        ("retrieve_knowledge", '{"query":"checkout","index":"other"}'),
        ("retrieve_knowledge", "not-json"),
    ],
)
def test_agent_cannot_select_a_tool_or_resource_outside_its_contract(name, arguments):
    api = Mock()
    api.responses.create.return_value = SimpleNamespace(
        status="completed",
        output=[SimpleNamespace(type="function_call", name=name, arguments=arguments)],
    )
    project = Mock()
    project.get_openai_client.return_value = nullcontext(api)
    with pytest.raises(ProtectionError):
        FoundryAgent(project, Mock(), SyntheticDetector()).answer(
            "support", "conversation", "HTTP 504"
        )


def test_prompt_definition_serializes_with_the_installed_sdk():
    definition = PromptAgentDefinition(
        model="fixture-model",
        instructions=INSTRUCTIONS,
        tools=[
            FunctionTool(
                name="retrieve_knowledge",
                parameters=TOOL_PARAMETERS,
                strict=False,
            )
        ],
    ).as_dict()
    assert definition["kind"] == "prompt"
    assert definition["tools"][0]["type"] == "function"
    assert definition["tools"][0]["strict"] is False
