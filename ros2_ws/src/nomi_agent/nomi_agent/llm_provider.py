"""Ollama and OpenAI-compatible provider adapter for NOMI."""
import json
import os
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Mapping
import xml.etree.ElementTree as ET

ENV_FILE = Path(os.environ.get("NOMI_AGENT_ENV", str(Path.home() / "ros2_ws/src/nomi_agent/.env"))).expanduser()
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE, override=True)
except ImportError:
    # Keep the local Ollama profile usable on ROS system Python installs that
    # do not have python-dotenv; Ark still checks for its SDK when selected.
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, separator, value = line.partition("=")
            if separator and key.strip():
                value = value.strip().strip('"').strip("'")
                os.environ[key.strip()] = value

DOUBAO_MODEL = "doubao-seed-2-0-pro-260215"
MODEL_PROFILES = {
    "qwen_hermes": {"router_provider": "ollama", "router_model": "qwen2.5:7b", "worker_provider": "ollama", "worker_model": "hermes3:8b"},
    # One model handles both intent interpretation and tool selection. The
    # single-model harness does not call route_command before tool calling.
    "qwen35_single": {"router_provider": "ollama", "router_model": "qwen3.5:9b", "worker_provider": "ollama", "worker_model": "qwen3.5:9b"},
    "doubao_pro": {"router_provider": "ark", "router_model": DOUBAO_MODEL, "worker_provider": "ark", "worker_model": DOUBAO_MODEL},
    "qwen_doubao": {"router_provider": "ollama", "router_model": "qwen2.5:7b", "worker_provider": "ark", "worker_model": DOUBAO_MODEL},
    "doubao_hermes": {"router_provider": "ark", "router_model": DOUBAO_MODEL, "worker_provider": "ollama", "worker_model": "hermes3:8b"},
}


@dataclass(frozen=True)
class NativeToolCall:
    """Normalized view of an Ollama tool call plus a NOMI correlation ID."""

    call_id: str
    name: str | None
    arguments: dict


@dataclass(frozen=True)
class NativeChatResponse:
    content: str
    tool_calls: list[NativeToolCall]
    assistant_message: dict


def initial_profile():
    profile = os.getenv("NOMI_MODEL_PROFILE", "qwen35_single").strip()
    if profile not in MODEL_PROFILES:
        raise ValueError(f"Unknown NOMI_MODEL_PROFILE: {profile}")
    return profile


def initial_config():
    """Honor the guide's explicit provider/model environment variables."""
    profile = initial_profile()
    config = dict(MODEL_PROFILES[profile])
    if profile == "qwen35_single":
        model = os.getenv("NOMI_SINGLE_MODEL", config["router_model"]).strip()
        config.update({"router_provider": "ollama", "router_model": model,
                       "worker_provider": "ollama", "worker_model": model})
        return profile, config
    config["router_provider"] = os.getenv("ROUTER_PROVIDER", config["router_provider"]).strip().lower()
    config["router_model"] = os.getenv("ROUTER_MODEL", config["router_model"]).strip()
    config["worker_provider"] = os.getenv("WORKER_PROVIDER", config["worker_provider"]).strip().lower()
    config["worker_model"] = os.getenv("WORKER_MODEL", config["worker_model"]).strip()
    return profile, config


class LLMProvider:
    def __init__(self, provider, model):
        self.provider = provider
        self.model = model
        try:
            self.timeout_seconds = max(1.0, float(os.getenv("NOMI_LLM_TIMEOUT_SECONDS", "60")))
        except ValueError:
            self.timeout_seconds = 60.0
        if provider == "ollama":
            import ollama
            host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
            if not host or host.endswith(".") or "://" not in host:
                host = "http://127.0.0.1:11434"
            # Verify host accessibility; fallback to local if remote unreachable
            try:
                test_client = ollama.Client(host=host, timeout=min(2.0, self.timeout_seconds))
                test_client.list()
                self.client = ollama.Client(host=host, timeout=self.timeout_seconds)
            except Exception:
                self.client = ollama.Client(host="http://127.0.0.1:11434", timeout=self.timeout_seconds)
        elif provider == "ark":
            from openai import OpenAI
            api_key = os.getenv("ARK_API_KEY", "").strip()
            if not api_key:
                raise ValueError("ARK_API_KEY is empty; add the Ark key to the local .env file")
            self.client = OpenAI(
                base_url=os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
                api_key=api_key,
                timeout=self.timeout_seconds,
                max_retries=0,
            )
        else:
            raise ValueError(f"Unsupported LLM provider: {provider}")

    def verify_available(self):
        if self.provider == "ollama":
            try:
                models_resp = self.client.list()
                models = [m.get("model") or m.get("name") for m in models_resp.get("models", [])]
                matched = any(self.model in (m or "") or (m or "").startswith(self.model) for m in models)
                if not matched:
                    installed = ", ".join(filter(None, models)) or "없음"
                    raise ValueError(f"Ollama에 '{self.model}' 모델이 없습니다 (설치됨: {installed}). 터미널에서 'ollama pull {self.model}' 실행 필요")
            except Exception as e:
                if isinstance(e, ValueError):
                    raise
                raise RuntimeError(f"Ollama 연결 실패: {e}")
        elif self.provider == "ark":
            api_key = os.getenv("ARK_API_KEY", "").strip()
            if not api_key:
                raise ValueError("Doubao(Ark) API 키(ARK_API_KEY)가 설정되지 않았습니다.")

    def chat(
        self,
        messages,
        json_mode=False,
        tools=None,
        think=None,
        keep_alive=None,
        options=None,
    ):
        if self.provider == "ollama":
            kwargs = {"model": self.model, "messages": messages}
            if json_mode:
                kwargs["format"] = "json"
            # Explicitly disable thinking (think=False) so Ollama Qwen models answer immediately without 40s internal soliloquy
            kwargs["think"] = think if think is not None else False
            if keep_alive is not None:
                kwargs["keep_alive"] = keep_alive
            if options is not None:
                kwargs["options"] = options
            if tools:
                kwargs["tools"] = tools
            response = self.client.chat(**kwargs)
            message = response.message
            content = message.content or ""
            # Normalize Ollama native tool calls to the same strict format
            # consumed by the existing parser (also used for Ark responses).
            for call in (getattr(message, "tool_calls", None) or []):
                function = call.function
                arguments = getattr(function, "arguments", {}) or {}
                if not isinstance(arguments, dict):
                    try:
                        arguments = json.loads(arguments)
                    except (TypeError, json.JSONDecodeError):
                        arguments = {"__invalid_json__": True}
                element = ET.Element("tool_call")
                element.text = json.dumps(
                    {"name": function.name, "arguments": arguments},
                    ensure_ascii=False,
                )
                content += "\n" + ET.tostring(element, encoding="unicode")
            return content

        # The existing worker loop stores tool results as Hermes-style role=tool
        # messages without OpenAI tool_call_id fields. Send those results as user
        # messages to keep the OpenAI-compatible conversation valid.
        api_messages = [
            ({"role": "user", "content": item.get("content", "")} if item.get("role") == "tool" else item)
            for item in messages
        ]
        kwargs = {"model": self.model, "messages": api_messages}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        response = self.client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        content = message.content or ""
        # Convert standard OpenAI tool calls into the existing Hermes format so
        # the established Robot Skills/Tool Registry execution path stays intact.
        for call in (message.tool_calls or []):
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                # Do not turn malformed input into empty arguments: that could
                # accidentally authorize a no-argument tool call.
                arguments = {"__invalid_json__": True}
            payload = {"name": call.function.name, "arguments": arguments}
            element = ET.Element("tool_call")
            element.text = json.dumps(payload, ensure_ascii=False)
            content += "\n" + ET.tostring(element, encoding="unicode")
        return content

    def chat_native(self, messages, tools, *, call_id_prefix, think=False, keep_alive="10m", options=None):
        """Call Ollama without flattening native function calls into XML text.

        Ollama 0.33.3's Message schema has no tool_call_id field. NOMI keeps a
        stable ID per returned call and places it in the corresponding tool
        result content; Ollama receives its supported role=tool/tool_name shape.
        """
        if self.provider != "ollama":
            raise ValueError("native Tool Calling is only supported by the Ollama provider")
        kwargs = {"model": self.model, "messages": messages, "tools": tools}
        if think is not None:
            kwargs["think"] = think
        if keep_alive is not None:
            kwargs["keep_alive"] = keep_alive
        if options is not None:
            kwargs["options"] = options

        response = self.client.chat(**kwargs)
        message = response.message
        content = getattr(message, "content", None) or ""
        calls = []
        native_payloads = []
        for index, call in enumerate(getattr(message, "tool_calls", None) or []):
            function = getattr(call, "function", None)
            name = getattr(function, "name", None) if function is not None else None
            raw_arguments = getattr(function, "arguments", None) if function is not None else None
            if isinstance(raw_arguments, Mapping):
                arguments = dict(raw_arguments)
            else:
                arguments = {"__invalid_arguments__": True}
            returned_id = getattr(call, "id", None)
            call_id = str(returned_id) if returned_id else f"{call_id_prefix}:call:{index + 1}"
            calls.append(NativeToolCall(call_id=call_id, name=name, arguments=arguments))
            if hasattr(call, "model_dump"):
                native_payloads.append(call.model_dump(exclude_none=True))
            elif isinstance(call, dict):
                native_payloads.append(dict(call))
            else:
                native_payloads.append({"function": {"name": name, "arguments": arguments}})

        assistant_message = {"role": "assistant", "content": content}
        if getattr(message, "thinking", None):
            assistant_message["thinking"] = message.thinking
        if native_payloads:
            assistant_message["tool_calls"] = native_payloads
        return NativeChatResponse(content=content, tool_calls=calls, assistant_message=assistant_message)
