"""Text browser over the storefront, for condition A of the eval.

It does what a browser tool does for a web agent: open a page, read it as
text, fill in a form and submit it. Hidden fields and preset values are sent
the way a browser sends them, so the model only types what a person would.
Offers the same tools()/body() pair as TrajanoClient, so llm_agent can drive
either one.
"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from types import TracebackType
from typing import Any, Self

import httpx

_BLOCKS = {"p", "div", "li", "tr", "table", "ol", "ul", "label", "header", "footer", "br"}
_HEADINGS = {"h1", "h2", "h3"}
_HIDDEN = {"head", "script", "style"}

_TOOLS = [
    {
        "name": "open_page",
        "description": "Open a page on this site and read it. Query strings are allowed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "For example / or /threads/12."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "submit_form",
        "description": (
            "Fill in and submit form N, from any page you have opened, then read the page "
            "it leads to. Form numbers are unique across pages. Hidden fields and preset "
            "values are sent for you: name only the fields you type into."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "form": {"type": "integer"},
                "fields": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["form"],
        },
    },
]


@dataclass
class Field:
    name: str
    kind: str
    value: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)


@dataclass
class Form:
    number: int
    method: str
    action: str
    fields: list[Field] = field(default_factory=list)
    submit: str = ""


class StorefrontBrowser:
    """Use as an async context manager. Logs in with the API key, like a person would.

    autofill names fields the browser fills in itself whenever a form has them,
    overriding the model, the way a password manager fills a saved secret.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        autofill: dict[str, str] | None = None,
        timeout_s: float = 30,
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.autofill = autofill or {}
        self.timeout_s = timeout_s
        self._http: httpx.AsyncClient | None = None
        # Every form seen this session, as if each page stayed open in its own tab. The
        # server still checks each submission, so a stale one fails with its own error.
        self._forms: dict[int, Form] = {}

    async def __aenter__(self) -> Self:
        self._http = httpx.AsyncClient(
            base_url=self.url, follow_redirects=True, timeout=httpx.Timeout(self.timeout_s)
        )
        response = await self._http.post("/login", data={"api_key": self.api_key})
        if response.status_code != 200:
            await self._http.aclose()
            raise RuntimeError(f"storefront login failed with HTTP {response.status_code}")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._http is not None:
            await self._http.aclose()
        self._http = None

    async def tools(self) -> list[dict[str, Any]]:
        return _TOOLS

    async def body(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One browser action. Errors come back in the body, for an LLM that reads them."""
        if self._http is None:
            raise RuntimeError("use StorefrontBrowser as an async context manager")
        if tool == "open_page":
            path = str(arguments.get("path", ""))
            if not path.startswith("/") or path.startswith("//"):
                return _error("VALIDATION", "path must start with / and stay on this site")
            return self._read(await self._http.get(path))
        if tool == "submit_form":
            return await self._submit(arguments)
        return _error("NOT_FOUND", f"unknown tool {tool}")

    async def _submit(self, arguments: dict[str, Any]) -> dict[str, Any]:
        assert self._http is not None
        number = arguments.get("form")
        form = self._forms.get(number) if isinstance(number, int) else None
        if form is None:
            return _error("NOT_FOUND", f"form {number} is not on any page you have opened")
        typed = {name: str(value) for name, value in (arguments.get("fields") or {}).items()}
        names = {item.name for item in form.fields}
        unknown = sorted(set(typed) - names)
        if unknown:
            return _error("VALIDATION", f"form {number} has no field {', '.join(unknown)}")
        data = {item.name: item.value for item in form.fields}
        data.update(typed)
        data.update({name: value for name, value in self.autofill.items() if name in names})
        if form.method == "GET":
            return self._read(await self._http.get(form.action, params=data))
        return self._read(await self._http.post(form.action, data=data))

    def _read(self, response: httpx.Response) -> dict[str, Any]:
        page = _Page(response.url.path, first_form=len(self._forms) + 1)
        page.feed(response.text)
        page.close()
        self._forms.update({form.number: form for form in page.forms})
        body: dict[str, Any] = {
            "url": response.url.raw_path.decode(),
            "status": response.status_code,
            "page": page.text(),
        }
        if response.status_code >= 400:
            body["error"] = {
                "code": page.error_code or f"HTTP_{response.status_code}",
                "message": page.error_message,
            }
        return body


def render(html: str, path: str = "/") -> tuple[str, list[Form]]:
    """The page as the model reads it, and the forms it can submit."""
    page = _Page(path)
    page.feed(html)
    page.close()
    return page.text(), page.forms


class _Page(HTMLParser):
    def __init__(self, path: str, first_form: int = 1) -> None:
        super().__init__(convert_charrefs=True)
        self.path = path
        self.first_form = first_form
        self.forms: list[Form] = []
        self.error_code = ""
        self.error_message = ""
        self._out: list[str] = []
        self._hidden = 0
        self._form: Form | None = None
        self._href: str | None = None
        self._cell = 0
        self._select: Field | None = None
        self._option: str | None = None
        self._textarea: Field | None = None
        self._button: list[str] | None = None
        self._in_error = False
        self._in_code = False

    def text(self) -> str:
        lines = (re.sub(r"[ \t]+", " ", line).strip() for line in "".join(self._out).split("\n"))
        return "\n".join(line for line in lines if line)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name: value or "" for name, value in attrs}
        if tag in _HIDDEN:
            self._hidden += 1
            return
        if self._hidden:
            return
        if tag in _BLOCKS or tag in _HEADINGS:
            self._out.append("\n")
        if tag in _HEADINGS:
            self._out.append("#" * int(tag[1]) + " ")
        if tag == "tr":
            self._cell = 0
        elif tag in ("td", "th"):
            if self._cell:
                self._out.append(" | ")
            self._cell += 1
        elif tag == "a":
            self._href = attr.get("href")
        elif tag == "form":
            self._open_form(attr)
        elif tag == "input":
            self._input(attr)
        elif tag == "select":
            self._select = self._add(attr, "select")
        elif tag == "option" and self._select is not None:
            self._option = attr.get("value", "")
            if self._option and (not self._select.value or "selected" in attr):
                self._select.value = self._option
            self._select.options.append(self._option)
        elif tag == "textarea":
            self._textarea = self._add(attr, "text")
        elif tag == "button":
            self._button = []
        elif tag == "strong" and self._in_error:
            self._in_code = True
        if "untrusted" in attr.get("class", "").split():
            self._out.append("(untrusted text) ")
        if tag == "p" and "error" in attr.get("class", "").split():
            self._in_error = True

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN:
            self._hidden = max(0, self._hidden - 1)
            return
        if self._hidden:
            return
        if tag == "a" and self._href is not None:
            self._out.append(f" [{self._href}]")
            self._href = None
        elif tag == "select" and self._select is not None:
            choices = ", ".join(self._select.options)
            self._out.append(f" [field {self._select.name}: one of {choices}]")
            self._select = None
        elif tag == "option":
            self._option = None
        elif tag == "textarea" and self._textarea is not None:
            required = ", required" if self._textarea.required else ""
            self._out.append(f" [field {self._textarea.name}: text{required}]")
            self._textarea = None
        elif tag == "button" and self._button is not None:
            label = " ".join("".join(self._button).split())
            if self._form is not None:
                self._form.submit = label
                self._out.append(f" [submit form {self._form.number}: {label}]")
            self._button = None
        elif tag == "form":
            self._form = None
        elif tag == "strong":
            self._in_code = False
        elif tag == "p":
            self._in_error = False
        if tag in _BLOCKS or tag in _HEADINGS:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._hidden or self._select is not None:
            return
        if self._textarea is not None:
            self._textarea.value += data
            return
        if self._button is not None:
            self._button.append(data)
            return
        if self._in_code:
            self.error_code += data.strip()
        elif self._in_error:
            self.error_message += " ".join(data.split())
        self._out.append(data.replace("\n", " "))

    def _open_form(self, attr: dict[str, str]) -> None:
        method = (attr.get("method") or "get").upper()
        action = attr.get("action") or self.path
        self._form = Form(number=self.first_form + len(self.forms), method=method, action=action)
        self.forms.append(self._form)
        self._out.append(f"\n[form {self._form.number}: {method} {action}]\n")

    def _input(self, attr: dict[str, str]) -> None:
        kind = attr.get("type", "text")
        item = self._add(attr, kind)
        if item is None or kind == "hidden":
            return
        details = [kind if kind != "text" else "", f'now "{item.value}"' if item.value else ""]
        if item.required:
            details.append("required")
        described = ", ".join(detail for detail in details if detail)
        self._out.append(f" [field {item.name}{': ' + described if described else ''}]")

    def _add(self, attr: dict[str, str], kind: str) -> Field | None:
        if self._form is None or not attr.get("name"):
            return None
        item = Field(
            name=attr["name"],
            kind=kind,
            value=attr.get("value", ""),
            required="required" in attr,
        )
        self._form.fields.append(item)
        return item


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}
