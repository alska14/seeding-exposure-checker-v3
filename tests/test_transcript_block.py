import sys
import types

import seeding_checker as sc


class IpBlocked(Exception):
    pass


class NoTranscriptFound(Exception):
    pass


def install_fake_api(monkeypatch, behavior):
    class Api:
        def fetch(self, vid, languages=None):
            r = behavior(vid)
            if isinstance(r, Exception):
                raise r
            return [types.SimpleNamespace(text=t, start=1.0) for t in r]

    mod = types.ModuleType("youtube_transcript_api")
    mod.YouTubeTranscriptApi = Api
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", mod)
    for k in sc.TR_STATS:
        sc.TR_STATS[k] = 0


def test_blocked_requests_are_counted_separately_from_missing_transcripts(monkeypatch, capsys):
    install_fake_api(monkeypatch, lambda v: {"a": ["브랜드 언급"], "b": NoTranscriptFound(), "c": IpBlocked()}[v])
    assert sc.get_transcript("a", ["ko"]) == "브랜드 언급"
    assert sc.get_transcript("b", ["ko"]) == "" and sc.get_transcript("c", ["ko"]) == ""
    assert sc.get_transcript_segments("c", ["ko"]) == []
    assert sc.TR_STATS == {"ok": 1, "none": 1, "blocked": 2, "other": 0}
    sc.print_tr_summary()
    out = capsys.readouterr().out
    assert "차단 2" in out and "확인하지 못함" in out


def test_no_warning_when_nothing_blocked(monkeypatch, capsys):
    install_fake_api(monkeypatch, lambda v: ["x"])
    sc.get_transcript("a", ["ko"])
    sc.print_tr_summary()
    assert "⚠" not in capsys.readouterr().out
