"""시험용 가짜 Claude 클라이언트. 네트워크를 쓰지 않는다."""

from __future__ import annotations

import json
from types import SimpleNamespace


def comments_in(kwargs) -> list[dict]:
    """요청의 사용자 메시지에 담긴 댓글 목록."""
    text = kwargs["messages"][0]["content"]
    start = text.index("<comments>") + len("<comments>")
    return json.loads(text[start:text.index("</comments>")])


def judge(item, **overrides) -> dict:
    """본문에 '욕'이 있으면 부정으로 보는 단순한 판정."""
    negative = "욕" in item["text"]
    result = {
        "id": item["id"],
        "sentiment": "부정" if negative else "중립",
        "target": "홍길동" if negative else "불명",
        "legal_type": "모욕" if negative else "해당 없음",
        "severity": 3 if negative else 0,
        "reason": "욕설" if negative else "잡담",
    }
    result.update(overrides)
    return result


def reply(results, *, stop_reason="end_turn", model="claude-opus-5-5", iterations=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps({"results": results}))],
        stop_reason=stop_reason,
        stop_details=None,
        model=model,
        usage=SimpleNamespace(
            input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
            cache_read_input_tokens=500, iterations=iterations,
        ),
    )


def refusal(model="claude-opus-5-5"):
    response = reply([], stop_reason="refusal", model=model)
    response.content = []
    return response


class FakeClaude:
    """handler(댓글 목록, 요청 인자)가 응답을 만든다. 기본은 전부 판정해 돌려준다."""

    def __init__(self, handler=None):
        self.handler = handler or (lambda items, kwargs: reply([judge(i) for i in items]))
        self.calls: list[dict] = []      # 요청 인자. "via"에 호출 경로가 들어간다.
        self.messages = SimpleNamespace(create=lambda **kw: self._create("messages", kw))
        self.beta = SimpleNamespace(
            messages=SimpleNamespace(create=lambda **kw: self._create("beta", kw))
        )

    def _create(self, via, kwargs):
        self.calls.append({"via": via, **kwargs})
        return self.handler(comments_in(kwargs), kwargs)

    @property
    def batches(self) -> list[list[int]]:
        return [[item["id"] for item in comments_in(call)] for call in self.calls]
