"""Tester för headless TP-login (coach/integrations/trainingpeaks/login.py).

Inga live-anrop — en fejkad requests.Session matar HTML/cookies så vi testar
CSRF-extraktion, cookie-fångst och felvägar (fel uppgifter, CAPTCHA, ändrad
login-sida).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coach.integrations.trainingpeaks.client import TPAuthError, TPError  # noqa: E402
from coach.integrations.trainingpeaks import login as L  # noqa: E402

_LOGIN_HTML = (
    '<form method="post"><input name="__RequestVerificationToken" '
    'type="hidden" value="CSRF-TOKEN-123"/>'
    '<input name="Username"/><input name="Password"/></form>'
)
_VALID_COOKIE = "p" * 80  # lång, ingen whitespace → valid_env_cookie ok


class _Cookie:
    def __init__(self, name, value):
        self.name = name
        self.value = value


class _FakeSession:
    """Minimal requests.Session-stand-in."""

    def __init__(self, *, page_html=_LOGIN_HTML, page_status=200,
                 set_cookie=None, post_html=""):
        self.headers = {}
        self.cookies = []
        self._page_html = page_html
        self._page_status = page_status
        self._set_cookie = set_cookie
        self._post_html = post_html
        self.posted = None

    def setdefault(self, *a):  # headers.setdefault proxy not needed
        pass

    def get(self, url, timeout=None):
        return _Resp(self._page_status, self._page_html)

    def post(self, url, data=None, timeout=None, headers=None, allow_redirects=True):
        self.posted = data
        if self._set_cookie:
            self.cookies.append(_Cookie(*self._set_cookie))
        return _Resp(200, self._post_html)


class _Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


def _run(name, fn):
    try:
        fn()
    except AssertionError as e:
        print(f"✗ {name}: {e}")
        return False
    print(f"✓ {name}")
    return True


def test_happy_path():
    sess = _FakeSession(set_cookie=(L.AUTH_COOKIE_NAME, _VALID_COOKIE))
    cookie = L.login_and_get_cookie("user@x.se", "pw", session=sess)
    assert cookie == _VALID_COOKIE, cookie
    # CSRF-token skickades med
    assert sess.posted["__RequestVerificationToken"] == "CSRF-TOKEN-123"
    assert sess.posted["Username"] == "user@x.se"
    # lösenordet finns bara i posten, lagras inte någon annanstans (modulen
    # returnerar bara cookien)
    assert sess.posted["Password"] == "pw"


def test_wrong_credentials_no_cookie():
    sess = _FakeSession(set_cookie=None, post_html="Fel lösenord")
    try:
        L.login_and_get_cookie("user@x.se", "bad", session=sess)
        assert False, "skulle höja TPAuthError"
    except TPAuthError as e:
        assert "nekades" in str(e).lower(), str(e)


def test_captcha_detected():
    sess = _FakeSession(set_cookie=None, post_html="Please complete the reCAPTCHA")
    try:
        L.login_and_get_cookie("user@x.se", "pw", session=sess)
        assert False, "skulle höja TPAuthError (captcha)"
    except TPAuthError as e:
        assert "captcha" in str(e).lower() or "verifiering" in str(e).lower(), str(e)


def test_login_page_changed_no_token():
    sess = _FakeSession(page_html="<html>ingen form här</html>")
    try:
        L.login_and_get_cookie("user@x.se", "pw", session=sess)
        assert False, "skulle höja TPError"
    except TPError as e:
        assert "login-formulär" in str(e).lower(), str(e)


def test_empty_credentials():
    try:
        L.login_and_get_cookie("", "pw", session=_FakeSession())
        assert False, "skulle höja TPAuthError"
    except TPAuthError:
        pass


def test_short_cookie_rejected():
    # TP satte en cookie men den ser ogiltig ut (för kort) → nekas
    sess = _FakeSession(set_cookie=(L.AUTH_COOKIE_NAME, "short"))
    try:
        L.login_and_get_cookie("user@x.se", "pw", session=sess)
        assert False, "skulle höja TPAuthError (ogiltig cookie)"
    except TPAuthError:
        pass


if __name__ == "__main__":
    ok = True
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            ok &= _run(name, fn)
    print("\n✓ ALLT GRÖNT" if ok else "\n✗ NÅGOT FALLERADE")
    raise SystemExit(0 if ok else 1)
