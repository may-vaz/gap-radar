"""ForgeOS core: live evidence, deterministic gates, optimization, chaos and certificates."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import networkx as nx
import numpy as np
import requests
from dotenv import load_dotenv
from ortools.sat.python import cp_model
from pydantic import BaseModel, Field, ValidationError, field_validator

ROOT = Path(__file__).resolve().parent
# Accept the conventional lowercase name and an uppercase variant used on some local setups.
load_dotenv(ROOT.parent / ".env")
load_dotenv(ROOT.parent / ".ENV")
load_dotenv(ROOT / ".env", override=True)
load_dotenv(ROOT / ".ENV", override=True)


class Component(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_\-]+$")
    name: str = Field(min_length=2, max_length=100)
    function: str = Field(min_length=3, max_length=240)
    search_query: str = Field(min_length=3, max_length=160)
    quantity_per_unit: int = Field(ge=1, le=100)
    must_include: list[str] = Field(default_factory=list, max_length=6)
    interface: str = Field(default="none", max_length=100)
    optional: bool = False
    alternatives: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("must_include", "alternatives")
    @classmethod
    def clean_list(cls, value: list[str]) -> list[str]:
        return [str(x).strip() for x in value if str(x).strip()]


class Dependency(BaseModel):
    source: str
    target: str
    rule: str = Field(min_length=2, max_length=200)


class Design(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    summary: str = Field(min_length=10, max_length=500)
    unit_name: str = Field(min_length=2, max_length=80)
    components: list[Component] = Field(min_length=2, max_length=8)
    dependencies: list[Dependency] = Field(default_factory=list, max_length=20)

    @field_validator("components")
    @classmethod
    def unique_components(cls, value: list[Component]) -> list[Component]:
        ids = [x.id for x in value]
        if len(ids) != len(set(ids)):
            raise ValueError("component ids must be unique")
        return value


class CompileRequest(BaseModel):
    intent: str = Field(min_length=12, max_length=1000)
    quantity: int = Field(ge=1, le=1000)
    budget: float = Field(gt=0, le=1_000_000_000)
    location: str = Field(min_length=2, max_length=120)
    deadline_days: int = Field(default=21, ge=1, le=365)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    selling_price_per_unit: float | None = Field(default=None, gt=0, le=100_000_000)
    include_local_fabrication: bool = True


class ForgeError(RuntimeError):
    def __init__(self, message: str, status: int = 400, details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


class SearchBudget:
    """Thread-safe, request-scoped cap for real SerpApi cache misses."""
    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.used = 0
        self.lock = threading.Lock()

    def reserve(self) -> None:
        with self.lock:
            if self.used >= self.limit:
                raise ForgeError(
                    f"Per-operation SerpApi limit of {self.limit} reached",
                    429,
                    {"operation_limit": self.limit, "live_searches_used": self.used},
                )
            self.used += 1


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or ROOT / "forgeos.db"
        self.lock = threading.RLock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, created TEXT, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, created REAL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS serp_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, month TEXT NOT NULL, request_hash TEXT NOT NULL)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()
        finally:
            db.close()

    def save_project(self, project_id: str, payload: dict[str, Any]) -> None:
        with self.lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO projects VALUES (?, ?, ?)",
                       (project_id, now_iso(), json.dumps(payload, ensure_ascii=False)))

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.lock, self._db() as db:
            row = db.execute("SELECT payload FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise ForgeError("Project not found", 404)
        return json.loads(row["payload"])

    def list_projects(self) -> list[dict[str, Any]]:
        with self.lock, self._db() as db:
            rows = db.execute("SELECT id, created, payload FROM projects ORDER BY created DESC LIMIT 20").fetchall()
        projects = []
        for row in rows:
            payload = json.loads(row["payload"])
            request = payload.get("request", {})
            projects.append({
                "id": row["id"],
                "created": row["created"],
                "name": payload.get("design", {}).get("name", "Project"),
                "intent": request.get("intent", ""),
                "quantity": request.get("quantity"),
                "budget": request.get("budget"),
                "location": request.get("location", ""),
                "demand_key": payload.get("demand_key") or project_demand_key(request),
                "is_repair": bool(payload.get("parent_project_id")),
            })
        return projects

    def cache_get(self, key: str, ttl: int) -> dict[str, Any] | None:
        with self.lock, self._db() as db:
            row = db.execute("SELECT created,payload FROM cache WHERE key=?", (key,)).fetchone()
        if row and time.time() - row["created"] <= ttl:
            return json.loads(row["payload"])
        return None

    def cache_put(self, key: str, payload: dict[str, Any]) -> None:
        with self.lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                       (key, time.time(), json.dumps(payload, ensure_ascii=False)))

    def serp_usage(self, monthly_limit: int, existing_usage: int, safety_reserve: int) -> dict[str, Any]:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self.lock, self._db() as db:
            local = int(db.execute("SELECT COUNT(*) FROM serp_usage WHERE month=?", (month,)).fetchone()[0])
        total = existing_usage + local
        return {
            "month": month, "monthly_limit": monthly_limit, "existing_usage_offset": existing_usage,
            "forgeos_live_searches": local, "estimated_used": total,
            "remaining": max(0, monthly_limit - total),
            "usable_remaining": max(0, monthly_limit - safety_reserve - total),
            "safety_reserve": safety_reserve,
        }

    def reserve_serp_search(self, request_hash: str, monthly_limit: int, existing_usage: int, safety_reserve: int) -> None:
        """Atomically reserve one live request before sending it; cache hits never call this."""
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            local = int(db.execute("SELECT COUNT(*) FROM serp_usage WHERE month=?", (month,)).fetchone()[0])
            if existing_usage + local >= monthly_limit - safety_reserve:
                raise ForgeError(
                    "SerpApi monthly safety budget exhausted; cached projects still remain available",
                    429,
                    {"month": month, "monthly_limit": monthly_limit, "estimated_used": existing_usage + local,
                     "safety_reserve": safety_reserve},
                )
            db.execute("INSERT INTO serp_usage(created, month, request_hash) VALUES (?, ?, ?)",
                       (now_iso(), month, request_hash))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def project_demand_key(request: dict[str, Any]) -> str:
    """Identify the physical demand represented by a plan, independent of reruns or repairs."""
    intent = re.sub(r"\s+", " ", str(request.get("intent", "")).strip().lower())
    location = re.sub(r"\s+", " ", str(request.get("location", "")).strip().lower())
    return canonical_hash({"intent": intent, "quantity": request.get("quantity"), "location": location})[:16]


def normalize_supplier_name(value: Any) -> str:
    """Collapse whitespace and case-only merchant variants into one display name."""
    raw = re.sub(r"\s+", " ", str(value or "Unknown merchant")).strip()
    key = raw.casefold()
    known = {"amazon.in": "Amazon.in", "amazon": "Amazon", "amazon india": "Amazon.in"}
    if key in known:
        return known[key]
    if raw.islower() or raw.isupper():
        return raw.title()
    return raw


def slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return value[:48] or "item"


class ForgeOS:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.serp_key = os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY") or ""
        self.ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
        self.model = os.getenv("OLLAMA_MODEL", "qwen3:4b")
        self.timeout = int(os.getenv("REQUEST_TIMEOUT", "300"))
        self.max_results = int(os.getenv("SHOPPING_RESULTS_PER_COMPONENT", "8"))
        self.cache_ttl = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
        self.max_components = int(os.getenv("MAX_COMPONENTS_PER_PROJECT", "6"))
        self.serp_monthly_limit = int(os.getenv("SERPAPI_MONTHLY_LIMIT", "250"))
        # Set this to searches already spent outside this ForgeOS database in the current month.
        self.serp_existing_usage = int(os.getenv("SERPAPI_EXISTING_MONTH_USAGE", "12"))
        self.serp_safety_reserve = int(os.getenv("SERPAPI_SAFETY_RESERVE", "5"))
        self.serp_operation_limit = int(os.getenv("SERPAPI_MAX_SEARCHES_PER_OPERATION", "10"))

    def quota(self) -> dict[str, Any]:
        status = self.store.serp_usage(self.serp_monthly_limit, self.serp_existing_usage, self.serp_safety_reserve)
        status["per_operation_limit"] = self.serp_operation_limit
        return status

    def health(self) -> dict[str, Any]:
        ollama = False
        models: list[str] = []
        try:
            response = requests.get(f"{self.ollama_url}/api/tags", timeout=3)
            if response.ok:
                models = [m.get("name", "") for m in response.json().get("models", [])]
                ollama = any(x == self.model or x.startswith(self.model + ":") for x in models)
        except requests.RequestException:
            pass
        return {
            "status": "ready" if self.serp_key and ollama else "configuration_required",
            "serpapi": bool(self.serp_key), "ollama": ollama, "model": self.model,
            "model_available": ollama, "python": f"{os.sys.version_info.major}.{os.sys.version_info.minor}",
            "engines": ["google_shopping", "google_maps"], "stored_projects": len(self.store.list_projects()),
            "serpapi_quota": self.quota(),
        }

    @staticmethod
    def _json_from_model(content: str) -> dict[str, Any]:
        """Parse JSON from strict output, markdown fences, or a Qwen think wrapper."""
        text = str(content or "").strip()
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.I).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("response did not contain a JSON object")
        value = json.loads(text[start:end + 1])
        if not isinstance(value, dict):
            raise ValueError("response JSON was not an object")
        return value

    def _ollama(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Generate a compact design without expensive JSON-schema grammar compilation.

        Some Ollama/Qwen combinations spend minutes compiling a nested JSON grammar before
        producing a token. We instead use JSON mode, a compact field contract, `/no_think`,
        and a bounded output. Pydantic remains the authoritative validator afterwards.
        """
        del schema  # Validation is performed by Pydantic; do not send the large schema to Qwen.
        contract = (
            'Return exactly one JSON object with: '
            '"name" string, "summary" string, "unit_name" string, '
            '"components" array of 4 to 6 objects containing '
            '"id", "name", "function", "search_query", "quantity_per_unit", '
            '"must_include", "interface", "optional", "alternatives"; '
            'and "dependencies" array of objects containing "source", "target", "rule". '
            'IDs must be lowercase letters, numbers, underscores or hyphens. '
            'must_include and alternatives are arrays of short strings. '
            'Every listed component is required, so optional must be false. quantity_per_unit is an integer.'
        )
        system = (
            "/no_think\nYou are a systems engineer. Produce concise machine-readable JSON only. "
            "Never invent prices, products, merchants, availability or test results. "
            "Use component categories that can be searched directly in Google Shopping."
        )
        user_prompt = f"/no_think\n{prompt}\n{contract}\nDo not include markdown or commentary."
        options = {
            "temperature": 0,
            "seed": 7,
            "num_ctx": 3072,
            "num_predict": 1200,
        }
        attempts = [
            (
                "/api/chat",
                {
                    "model": self.model,
                    "stream": False,
                    "think": False,
                    "keep_alive": "30m",
                    "format": "json",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_prompt},
                    ],
                    "options": options,
                },
                "chat JSON mode",
            ),
            (
                "/api/generate",
                {
                    "model": self.model,
                    "stream": False,
                    "keep_alive": "30m",
                    "format": "json",
                    "prompt": f"{system}\n{user_prompt}",
                    "options": options,
                },
                "generate JSON mode",
            ),
        ]
        failures: list[str] = []
        per_attempt_timeout = max(30, min(150, self.timeout // len(attempts)))
        for endpoint, payload, label in attempts:
            try:
                res = requests.post(
                    f"{self.ollama_url}{endpoint}",
                    json=payload,
                    timeout=(10, per_attempt_timeout),
                )
            except requests.Timeout:
                failures.append(f"{label} timed out after {per_attempt_timeout}s")
                continue
            except requests.RequestException as exc:
                failures.append(f"{label} failed: {type(exc).__name__}")
                continue
            if not res.ok:
                failures.append(f"{label} returned HTTP {res.status_code}: {res.text[:160].strip()}")
                continue
            try:
                body = res.json()
                content = body.get("message", {}).get("content") or body.get("response", "")
                return self._json_from_model(content)
            except (ValueError, requests.RequestException) as exc:
                failures.append(f"{label} returned invalid JSON: {exc}")
        summary = "; ".join(failures)[:700] or "no response content"
        raise ForgeError(
            f"Local Qwen inference failed in both compact API modes: {summary}. "
            f"Verify independently with: ollama run {self.model} '/no_think Reply READY'",
            503,
        )

    def _complete_required_components(self, design: Design) -> Design:
        """Apply a deterministic completeness floor when a small model under-decomposes a kit."""
        components = [c.model_copy(update={"optional": False}) for c in design.components]
        if len(components) >= 4:
            return design.model_copy(update={"components": components})
        existing_text = " ".join(f"{c.id} {c.name} {c.function}" for c in components).lower()
        roles = [
            ("control_unit", "Control unit", "Coordinates sensing, outputs and system behavior", f"{design.name} microcontroller control board", ["controller", "microcontroller", "processor"], "digital"),
            ("power_system", "Power system", "Provides regulated power to the complete system", f"{design.name} regulated power supply module", ["power", "battery", "solar", "supply"], "power"),
            ("protective_enclosure", "Protective enclosure", "Protects and physically integrates the installed components", f"{design.name} electronics enclosure", ["enclosure", "housing", "case", "chassis"], "physical"),
            ("user_interface", "User interface", "Communicates system status and alerts to the operator", f"{design.name} display alert module", ["display", "alert", "buzzer", "interface"], "digital"),
        ]
        used_ids = {c.id for c in components}
        for base_id, name, function, query, coverage, interface in roles:
            if len(components) >= 4:
                break
            if any(token in existing_text for token in coverage):
                continue
            component_id = base_id
            suffix = 2
            while component_id in used_ids:
                component_id = f"{base_id}_{suffix}"; suffix += 1
            components.append(Component(
                id=component_id, name=name, function=function, search_query=query,
                quantity_per_unit=1, must_include=[], interface=interface,
                optional=False, alternatives=[],
            ))
            used_ids.add(component_id)
            existing_text += f" {component_id} {name.lower()} {function.lower()}"
        return design.model_copy(update={"components": components})

    def design(self, req: CompileRequest) -> Design:
        prompt = f"""Create a minimal complete functional bill of materials for this project.
Intent: {req.intent}
Production quantity: {req.quantity} {req.currency}; total budget: {req.budget}; location: {req.location}; deadline: {req.deadline_days} days.
Return 4-{self.max_components} purchasable component categories that form a complete working system, not brands or merchant listings. Include control, sensing or actuation, power and physical integration categories when the requested system needs them. Every listed component is required, so set optional to false. Every search_query must be precise enough for Google Shopping. quantity_per_unit is per finished unit. must_include contains only short tokens that should literally occur in a suitable listing (model/interface/voltage). Dependencies must connect components whose operation depends on each other and must reference component ids. Do not include labor, vague consumables, software, prices, suppliers, or fabricated facts."""
        schema = Design.model_json_schema()
        schema["properties"]["components"]["minItems"] = 4
        schema["properties"]["components"]["maxItems"] = self.max_components
        raw = self._ollama(prompt, schema)
        try:
            design = Design.model_validate(raw)
            if len(design.components) > self.max_components:
                raise ValueError(f"design exceeds the {self.max_components}-component search budget")
            return self._complete_required_components(design)
        except (ValidationError, ValueError) as first_error:
            repair_prompt = (
                "Correct the following proposed design so it satisfies the schema. Preserve the project intent, "
                f"use no more than {self.max_components} components, and return only the corrected JSON.\n"
                f"Validation error: {first_error}\nProposed design: {json.dumps(raw)}"
            )
            try:
                repaired = Design.model_validate(self._ollama(repair_prompt, schema))
                if len(repaired.components) > self.max_components:
                    raise ValueError(f"design exceeds the {self.max_components}-component search budget")
                return self._complete_required_components(repaired)
            except (ValidationError, ValueError) as final_error:
                details = final_error.errors() if isinstance(final_error, ValidationError) else str(final_error)
                raise ForgeError("Qwen returned an invalid system design after one repair attempt", 502, details)

    def _serp(self, params: dict[str, Any], budget: SearchBudget | None = None) -> dict[str, Any]:
        if not self.serp_key:
            raise ForgeError("SERPAPI_API_KEY is missing from .env", 503)
        safe_params = dict(params)
        key = canonical_hash(safe_params)
        cached = self.store.cache_get(key, self.cache_ttl)
        if cached:
            cached["_cache_hit"] = True
            return cached
        # Cache misses consume both the request-scoped budget and monthly ledger.
        if budget is not None:
            budget.reserve()
        self.store.reserve_serp_search(
            key, self.serp_monthly_limit, self.serp_existing_usage, self.serp_safety_reserve
        )
        params = {**params, "api_key": self.serp_key}
        try:
            res = requests.get("https://serpapi.com/search.json", params=params, timeout=self.timeout)
            if not res.ok:
                # Never surface the prepared request URL: it contains the API key.
                raise ForgeError(f"SerpApi returned HTTP {res.status_code}", 502)
            data = res.json()
        except ForgeError:
            raise
        except (requests.RequestException, ValueError):
            raise ForgeError("SerpApi network request failed", 502)
        if data.get("error"):
            raise ForgeError(f"SerpApi rejected the search: {data['error']}", 502)
        data["_cache_hit"] = False
        self.store.cache_put(key, data)
        return data

    @staticmethod
    def _pack_size(text: str) -> int:
        patterns = [r"pack\s+of\s+(\d+)", r"(\d+)\s*[- ]?pack\b", r"set\s+of\s+(\d+)", r"\b(\d+)\s*pcs?\b"]
        for pattern in patterns:
            hit = re.search(pattern, text, re.I)
            if hit:
                return max(1, min(1000, int(hit.group(1))))
        return 1

    def search_component(self, component: Component, req: CompileRequest, budget: SearchBudget) -> dict[str, Any]:
        query = f"{component.search_query} buy {req.location}"
        data = self._serp({"engine": "google_shopping", "q": query, "gl": "in", "hl": "en", "location": req.location}, budget)
        candidates = []
        for idx, item in enumerate(data.get("shopping_results", [])[:self.max_results]):
            price = item.get("extracted_price")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            evidence_text = " ".join([str(item.get("title", "")), str(item.get("snippet", "")), " ".join(map(str, item.get("extensions", [])))])
            lowered = evidence_text.lower()
            checks = [{"term": term, "passed": term.lower() in lowered} for term in component.must_include]
            matched = sum(x["passed"] for x in checks)
            coverage = matched / len(checks) if checks else 1.0
            rating = float(item.get("rating") or 0)
            reviews = int(item.get("reviews") or 0)
            delivery_known = bool(item.get("delivery"))
            evidence_risk = 0.42 * (1 - coverage) + (0.18 if not delivery_known else 0) + (0.18 if reviews < 10 else 0) + (0.12 if rating and rating < 3.8 else 0)
            evidence_risk = round(min(0.92, max(0.04, evidence_risk)), 3)
            product_ref = str(item.get("product_id") or item.get("immersive_product_page_token") or f"{idx}-{canonical_hash(item)[:8]}")
            candidates.append({
                "id": f"{component.id}:{product_ref[:40]}", "component_id": component.id,
                "title": item.get("title", "Untitled product"), "supplier": normalize_supplier_name(item.get("source")),
                "price": round(float(price), 2), "price_text": item.get("price", str(price)),
                "pack_size": self._pack_size(evidence_text), "rating": rating or None, "reviews": reviews,
                "delivery": item.get("delivery"), "link": item.get("product_link") or item.get("link"),
                "thumbnail": item.get("serpapi_thumbnail") or item.get("thumbnail"),
                "product_id": item.get("product_id"), "compatibility": "verified" if coverage == 1 else "needs_review",
                "gates": checks, "evidence_risk": evidence_risk,
            })
        return {
            "component_id": component.id, "query": query, "candidates": candidates,
            "search_id": data.get("search_metadata", {}).get("id"), "searched_at": now_iso(),
            "cache_hit": data.get("_cache_hit", False), "result_count": len(candidates),
        }

    def market(self, design: Design, req: CompileRequest, budget: SearchBudget, reuse: dict[str, Any] | None = None) -> dict[str, Any]:
        reuse = reuse or {}
        evidence: dict[str, Any] = {}
        pending: list[Component] = []
        for c in design.components:
            old = reuse.get(c.id)
            if old and old.get("query", "").startswith(c.search_query):
                evidence[c.id] = old
            else:
                pending.append(c)
        with ThreadPoolExecutor(max_workers=min(5, len(pending) or 1)) as pool:
            jobs = {pool.submit(self.search_component, c, req, budget): c.id for c in pending}
            for future in as_completed(jobs):
                evidence[jobs[future]] = future.result()
        missing = [c.name for c in design.components if not c.optional and not evidence.get(c.id, {}).get("candidates")]
        if missing:
            raise ForgeError("No priced Shopping evidence for required components", 422, {"missing": missing})
        return evidence

    @staticmethod
    def graph(design: Design) -> dict[str, Any]:
        graph = nx.DiGraph()
        for c in design.components:
            graph.add_node(c.id, label=c.name, function=c.function, optional=c.optional)
        valid_ids = set(graph.nodes)
        rejected = []
        for edge in design.dependencies:
            if edge.source in valid_ids and edge.target in valid_ids:
                graph.add_edge(edge.source, edge.target, rule=edge.rule)
            else:
                rejected.append(edge.model_dump())
        cycles = list(nx.simple_cycles(graph))
        criticality = {n: len(nx.descendants(graph, n)) + graph.out_degree(n) for n in graph.nodes}
        return {
            "nodes": [{"id": n, **graph.nodes[n], "criticality": criticality[n]} for n in graph.nodes],
            "edges": [{"source": a, "target": b, **d} for a, b, d in graph.edges(data=True)],
            "cycles": cycles, "rejected_edges": rejected,
        }

    def optimize(self, design: Design, market: dict[str, Any], req: CompileRequest, strategy: Literal["cheapest", "balanced", "resilient"], blocked_suppliers: set[str] | None = None, supplier_cap: int | None = None) -> dict[str, Any]:
        blocked = {x.lower() for x in (blocked_suppliers or set())}
        model = cp_model.CpModel()
        variables: dict[str, Any] = {}
        rows: list[tuple[Component, dict[str, Any], int, int]] = []
        supplier_vars: dict[str, list[Any]] = {}
        for comp in design.components:
            usable = [x for x in market[comp.id]["candidates"] if x["supplier"].lower() not in blocked]
            if not usable and not comp.optional:
                return {"strategy": strategy, "status": "infeasible", "reason": f"No candidate remains for {comp.name}"}
            comp_vars = []
            demand = req.quantity * comp.quantity_per_unit
            for source_candidate in usable:
                candidate = dict(source_candidate)
                candidate["supplier"] = normalize_supplier_name(candidate.get("supplier"))
                packages = math.ceil(demand / candidate["pack_size"])
                cents = int(round(candidate["price"] * packages * 100))
                var = model.new_bool_var(slug(candidate["id"]))
                variables[candidate["id"]] = var
                comp_vars.append(var)
                rows.append((comp, candidate, packages, cents))
                supplier_vars.setdefault(candidate["supplier"], []).append(var)
            if comp_vars:
                model.add(sum(comp_vars) == (0 if comp.optional else 1) if False else sum(comp_vars) <= 1)
                if not comp.optional:
                    model.add(sum(comp_vars) == 1)
        if not rows:
            return {"strategy": strategy, "status": "infeasible", "reason": "No market candidates"}
        if supplier_cap is not None:
            for vals in supplier_vars.values():
                model.add(sum(vals) <= max(1, supplier_cap))
        total_cost = sum(cents * variables[c["id"]] for _, c, _, cents in rows)
        model.add(total_cost <= int(req.budget * 100))
        max_exposure = model.new_int_var(0, len(design.components), "max_supplier_exposure")
        for vals in supplier_vars.values():
            model.add(max_exposure >= sum(vals))
        risk_term = sum(int(c["evidence_risk"] * 1000) * variables[c["id"]] for _, c, _, _ in rows)
        # Risk and concentration must be large enough to produce a genuinely different
        # recommendation when safer evidence exists; cost remains a hard budget constraint.
        weights = {
            "cheapest": (1, 1, 100),
            "balanced": (1, 100, 50_000),
            "resilient": (1, 500, 250_000),
        }[strategy]
        model.minimize(weights[0] * total_cost + weights[1] * risk_term + weights[2] * max_exposure)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 8
        solver.parameters.num_search_workers = 8
        status = solver.solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return {"strategy": strategy, "status": "infeasible", "reason": "Budget or availability constraints cannot be satisfied"}
        selections = []
        for comp, candidate, packages, cents in rows:
            if solver.value(variables[candidate["id"]]):
                selections.append({**candidate, "component_name": comp.name, "required_units": req.quantity * comp.quantity_per_unit,
                                   "packages": packages, "supplied_units": packages * candidate["pack_size"], "line_cost": cents / 100})
        total = round(sum(x["line_cost"] for x in selections), 2)
        supplier_counts: dict[str, int] = {}
        for x in selections:
            supplier_counts[x["supplier"]] = supplier_counts.get(x["supplier"], 0) + 1
        return {
            "strategy": strategy, "status": "optimal" if status == cp_model.OPTIMAL else "feasible",
            "total_cost": total, "budget_remaining": round(req.budget - total, 2), "selections": selections,
            "supplier_count": len(supplier_counts), "max_supplier_exposure": max(supplier_counts.values(), default=0),
            "supplier_mix": supplier_counts, "solver": "OR-Tools CP-SAT", "objective_bound": solver.best_objective_bound,
        }

    @staticmethod
    def chaos(plan: dict[str, Any], deadline_days: int, scenarios: int = 5000) -> dict[str, Any]:
        if plan.get("status") not in {"optimal", "feasible"}:
            return {"scenarios": 0, "completion_probability": 0, "reason": "No feasible plan"}
        seed = int(canonical_hash(plan)[:8], 16)
        rng = np.random.default_rng(seed)
        picks = plan["selections"]
        n = max(500, min(20000, scenarios))
        total_costs = np.zeros(n)
        complete = np.ones(n, dtype=bool)
        on_time = np.ones(n, dtype=bool)
        failed_by_supplier: dict[str, np.ndarray] = {}
        for item in picks:
            risk = float(item["evidence_risk"])
            supplier = item["supplier"]
            if supplier not in failed_by_supplier:
                systemic = rng.random(n) < (0.015 + risk * 0.06)
                failed_by_supplier[supplier] = systemic
            available = ~(failed_by_supplier[supplier] | (rng.random(n) < (0.02 + risk * 0.18)))
            inflation = rng.lognormal(mean=0.02 + risk * 0.09, sigma=0.05 + risk * 0.17, size=n)
            recovery = np.where(available, 1.0, rng.uniform(1.12, 1.55, n))
            total_costs += item["line_cost"] * inflation * recovery
            complete &= available
            base_delay = 4 if item.get("delivery") else 9
            delay = rng.gamma(shape=2.0, scale=base_delay * (0.45 + risk), size=n)
            on_time &= delay <= deadline_days
        p95 = float(np.percentile(total_costs, 95))
        tail = total_costs[total_costs >= p95]
        base = float(plan["total_cost"])
        supplier_cost: dict[str, float] = {}
        for x in picks:
            supplier_cost[x["supplier"]] = supplier_cost.get(x["supplier"], 0) + x["line_cost"]
        worst_supplier = max(supplier_cost, key=supplier_cost.get) if supplier_cost else None
        hardening = min(picks, key=lambda x: (x["line_cost"] * (0.35 + x["evidence_risk"]))) if picks else None
        return {
            "scenarios": n, "seed": seed, "completion_probability": round(float(np.mean(complete)), 4),
            "on_time_probability": round(float(np.mean(on_time)), 4), "p95_cost": round(p95, 2),
            "cvar95": round(float(np.mean(tail)), 2), "expected_cost": round(float(np.mean(total_costs)), 2),
            "expected_overrun": round(float(np.mean(total_costs)) - base, 2),
            "blast_radius": {"supplier": worst_supplier, "cost_share": round(supplier_cost.get(worst_supplier, 0) / base, 3) if base and worst_supplier else 0},
            "minimum_cost_hardening": ({"component": hardening["component_name"], "action": "qualify a second supplier and hold one reserve pack",
                                         "estimated_reserve_cost": hardening["price"]} if hardening else None),
            "assumptions": "Seeded stress model; probabilities are decision-support estimates derived from evidence completeness, not supplier guarantees.",
        }

    @staticmethod
    def readiness(plan: dict[str, Any], chaos: dict[str, Any], req: CompileRequest) -> dict[str, Any]:
        """Deterministic release gate with an explainable 0-100 score and action list."""
        if plan.get("status") not in {"optimal", "feasible"}:
            return {"score": 0, "verdict": "NOT_READY", "gates": [], "actions": [plan.get("reason", "No feasible procurement plan")]}
        selections = plan.get("selections", [])
        verified = sum(x.get("compatibility") == "verified" for x in selections)
        evidence_ratio = verified / len(selections) if selections else 0
        completion = float(chaos.get("completion_probability", 0))
        on_time = float(chaos.get("on_time_probability", 0))
        supplier_count = int(plan.get("supplier_count", 0))
        max_exposure = int(plan.get("max_supplier_exposure", 0))
        diversity = 1.0 if supplier_count >= 2 else 0.35
        headroom_ratio = max(0.0, min(1.0, float(plan.get("budget_remaining", 0)) / max(req.budget, 1)))
        score = round(40 * completion + 20 * on_time + 20 * evidence_ratio + 10 * diversity + 10 * headroom_ratio)
        gates = [
            {"name": "Budget", "passed": plan.get("total_cost", req.budget + 1) <= req.budget, "detail": f"{plan.get('budget_remaining', 0):.2f} budget remaining"},
            {"name": "Component coverage", "passed": bool(selections), "detail": f"{len(selections)} required categories selected"},
            {"name": "Compatibility evidence", "passed": evidence_ratio == 1, "detail": f"{verified} of {len(selections)} selected items verified"},
            {"name": "Supplier resilience", "passed": supplier_count >= 2 and max_exposure < len(selections), "detail": f"{supplier_count} suppliers; maximum exposure {max_exposure} components"},
            {"name": "Completion probability", "passed": completion >= 0.85, "detail": f"{completion * 100:.0f}% simulated completion"},
        ]
        actions = []
        if evidence_ratio < 1: actions.append("Confirm specifications for every item marked Review required.")
        if supplier_count < 2: actions.append("Qualify at least one additional supplier before purchase approval.")
        if completion < 0.85: actions.append("Apply the recommended reserve or alternative-supplier hardening action.")
        if headroom_ratio < 0.1: actions.append("Add budget contingency for price and delivery variation.")
        verdict = "READY" if score >= 85 and all(g["passed"] for g in gates) else "CONDITIONAL" if score >= 60 else "NOT_READY"
        return {"score": score, "verdict": verdict, "gates": gates, "actions": actions or ["No blocking action remains; retain evidence before purchase approval."]}

    @staticmethod
    def economics(plan: dict[str, Any], req: CompileRequest) -> dict[str, Any]:
        """Explain affordability and, when provided, material-level gross profit."""
        if plan.get("status") not in {"optimal", "feasible"}:
            return {"available": False, "reason": "No feasible procurement plan"}
        cost = round(float(plan["total_cost"]), 2)
        break_even = round(cost / req.quantity, 2)
        result: dict[str, Any] = {
            "available": True,
            "estimated_material_cost": cost,
            "budget": req.budget,
            "budget_headroom": round(req.budget - cost, 2),
            "break_even_material_cost_per_unit": break_even,
            "selling_price_provided": req.selling_price_per_unit is not None,
            "scope_note": "Gross profit includes sourced material cost only; labor, tax, shipping and operating costs are not included unless present in market evidence.",
        }
        if req.selling_price_per_unit is not None:
            revenue = round(req.selling_price_per_unit * req.quantity, 2)
            profit = round(revenue - cost, 2)
            result.update({
                "selling_price_per_unit": req.selling_price_per_unit,
                "projected_revenue": revenue,
                "projected_gross_profit": profit,
                "gross_margin_percent": round((profit / revenue) * 100, 2) if revenue else 0,
                "profitable_on_material_cost": profit > 0,
            })
        return result

    def commercial_viability(self, design: Design, req: CompileRequest, plan: dict[str, Any], budget: SearchBudget) -> dict[str, Any]:
        """Benchmark the finished product against current market offers using one search."""
        product_name = re.sub(r"\b(BOM|bill of materials)\b", "", design.name, flags=re.I).strip()
        query = f"{product_name} complete {design.unit_name} buy {req.location}"
        try:
            data = self._serp({"engine": "google_shopping", "q": query, "gl": "in", "hl": "en", "location": req.location}, budget)
        except ForgeError as exc:
            return {"available": False, "query": query, "warning": str(exc), "comparables": []}
        comparables = []
        for item in data.get("shopping_results", [])[:12]:
            price = item.get("extracted_price")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            comparables.append({
                "title": item.get("title", "Comparable product"),
                "supplier": normalize_supplier_name(item.get("source")),
                "price": round(float(price), 2),
                "rating": item.get("rating"),
                "reviews": item.get("reviews"),
                "link": item.get("product_link") or item.get("link"),
            })
        if not comparables:
            return {"available": False, "query": query, "search_id": data.get("search_metadata", {}).get("id"),
                    "warning": "No priced finished-product comparables were returned", "comparables": []}
        prices = np.array([x["price"] for x in comparables], dtype=float)
        low, median, high = [round(float(x), 2) for x in np.percentile(prices, [25, 50, 75])]
        economics = self.economics(plan, req)
        target = req.selling_price_per_unit
        if target is None:
            verdict = "PRICE_INPUT_REQUIRED"
            explanation = f"Comparable products have a median listed price of {median:.2f}. Enter an expected selling price to calculate margin and market position."
            percentile = None
        else:
            percentile = round(float(np.mean(prices <= target) * 100), 1)
            profit = float(economics.get("projected_gross_profit", 0))
            if profit <= 0:
                verdict = "MATERIAL_MARGIN_NEGATIVE"
                explanation = "The entered selling price does not cover the optimized material cost. Raise the price or reduce the build cost before launch."
            elif target > high:
                verdict = "PREMIUM_POSITION_REQUIRES_DIFFERENTIATION"
                explanation = "The proposed price is above the upper market quartile. A launch may be viable, but the product needs clear feature, service or quality differentiation."
            elif target < low:
                verdict = "COMPETITIVE_BUT_CHECK_UNDERPRICING"
                explanation = "The proposed price is below the lower market quartile and covers material cost. Confirm that labor, tax, warranty and operating costs still leave an acceptable margin."
            else:
                verdict = "COMMERCIALLY_PLAUSIBLE"
                explanation = "The proposed price sits within the central market range and covers the optimized material cost. Complete the flagged technical reviews before launch."
        return {
            "available": True, "query": query, "search_id": data.get("search_metadata", {}).get("id"),
            "comparable_count": len(comparables), "market_price_low": low, "market_price_median": median,
            "market_price_high": high, "selling_price": target, "price_percentile": percentile,
            "verdict": verdict, "explanation": explanation, "economics": economics,
            "comparables": sorted(comparables, key=lambda x: x["price"])[:6],
            "scope_note": "Listed market prices are directional evidence, not verified sales volume or demand forecasts.",
        }

    def local_fabricators(self, req: CompileRequest, budget: SearchBudget) -> dict[str, Any]:
        if not req.include_local_fabrication:
            return {"searched": False, "results": []}
        try:
            data = self._serp({"engine": "google_maps", "type": "search", "q": f"electronics prototyping fabrication services in {req.location}", "hl": "en"}, budget)
        except ForgeError as exc:
            # This is an optional enrichment and must not invalidate an otherwise feasible build plan.
            return {"searched": False, "results": [], "warning": str(exc)}
        results = []
        for place in data.get("local_results", [])[:6]:
            results.append({k: place.get(k) for k in ("title", "type", "address", "phone", "rating", "reviews", "website")})
        return {"searched": True, "search_id": data.get("search_metadata", {}).get("id"), "results": results,
                "decision": "Capability leads only; request a quote before make-versus-buy comparison."}

    def _certificate(self, payload: dict[str, Any]) -> dict[str, Any]:
        binding = {"design": payload["design"], "evidence": payload["evidence"], "plans": payload["plans"], "chaos": payload["chaos"], "readiness": payload.get("readiness", {}), "economics": payload.get("economics", {}), "commercial": payload.get("commercial", {}), "scenario": payload.get("scenario", {})}
        digest = canonical_hash(binding)
        feasible = [x for x in payload["plans"] if x.get("status") in {"optimal", "feasible"}]
        return {
            "certificate_id": f"build-{digest[:16]}", "schema_version": "1.0", "issued_at": now_iso(),
            "sha256": digest, "verdict": "BUILDABLE_WITH_REVIEW" if feasible else "NOT_BUILDABLE",
            "design_hash": canonical_hash(payload["design"]), "evidence_hash": canonical_hash(payload["evidence"]),
            "solver": "OR-Tools CP-SAT 9.15", "evidence_source": "SerpApi Google Shopping / Maps",
            "passed": ["priced_candidate_per_required_component", "budget_constraint", "quantity_and_pack_coverage"] if feasible else [],
            "warnings": ["Some selected products require manual specification confirmation.", "Prices and availability can change; refresh the plan before purchasing."],
        }

    def compile(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            req = CompileRequest.model_validate(raw)
        except ValidationError as exc:
            raise ForgeError("Invalid project request", 422, exc.errors())
        budget = SearchBudget(self.serp_operation_limit)
        design = self.design(req)
        evidence = self.market(design, req, budget)
        plans = [self.optimize(design, evidence, req, x) for x in ("cheapest", "balanced", "resilient")]
        preferred = next((x for x in plans if x["strategy"] == "balanced" and x.get("status") in {"optimal", "feasible"}), plans[0])
        chaos = self.chaos(preferred, req.deadline_days)
        readiness = self.readiness(preferred, chaos, req)
        commercial = self.commercial_viability(design, req, preferred, budget)
        fabrication = self.local_fabricators(req, budget)
        payload = {
            "project_id": uuid.uuid4().hex[:12], "created_at": now_iso(),
            "demand_key": project_demand_key(req.model_dump()), "request": req.model_dump(),
            "design": design.model_dump(), "graph": self.graph(design), "evidence": evidence, "plans": plans,
            "chaos": chaos, "readiness": readiness, "economics": self.economics(preferred, req),
            "commercial": commercial, "fabrication": fabrication,
            "operation_searches": {"live_searches": budget.used, "limit": budget.limit},
        }
        payload["serpapi_quota"] = self.quota()
        payload["certificate"] = self._certificate(payload)
        self.store.save_project(payload["project_id"], payload)
        return payload

    def heal(self, project_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        old = self.store.get_project(project_id)
        supplier = str(raw.get("blocked_supplier") or "").strip()
        if not supplier:
            plan = next((p for p in old["plans"] if p.get("status") in {"optimal", "feasible"}), None)
            if not plan or not plan.get("selections"):
                raise ForgeError("No supplier is available to disrupt", 422)
            supplier = max(plan["supplier_mix"], key=plan["supplier_mix"].get)
        req = CompileRequest.model_validate(old["request"])
        previous = Design.model_validate(old["design"])
        affected = [c.model_dump() for c in previous.components if any(x["supplier"].lower() == supplier.lower() for x in old["evidence"][c.id]["candidates"])]
        prompt = f"""Redesign this complete system after supplier/market failure. Preserve project function and keep unaffected component ids where possible.
Project request: {json.dumps(old['request'])}
Current design: {json.dumps(old['design'])}
Failed supplier: {supplier}
Affected categories: {json.dumps(affected)}
Make at least one genuine design-level technical substitution for an affected category, update its search query, interfaces, downstream dependencies, and alternatives. Return the complete revised design. Do not invent products, prices, suppliers, or availability."""
        repair_schema = Design.model_json_schema()
        repair_schema["properties"]["components"]["minItems"] = 4
        repair_schema["properties"]["components"]["maxItems"] = self.max_components
        try:
            revised = Design.model_validate(self._ollama(prompt, repair_schema))
            if not 4 <= len(revised.components) <= self.max_components:
                raise ValueError(f"repair design must contain 4-{self.max_components} components")
            revised = revised.model_copy(update={"components": [c.model_copy(update={"optional": False}) for c in revised.components]})
        except (ValidationError, ValueError) as exc:
            details = exc.errors() if isinstance(exc, ValidationError) else str(exc)
            raise ForgeError("Qwen returned an incomplete repair design", 502, details)
        budget = SearchBudget(self.serp_operation_limit)
        evidence = self.market(revised, req, budget, old["evidence"])
        plans = [self.optimize(revised, evidence, req, s, {supplier}) for s in ("cheapest", "balanced", "resilient")]
        chosen = next((x for x in plans if x.get("status") in {"optimal", "feasible"}), plans[0])
        changes = []
        before = {c.id: c for c in previous.components}
        for comp in revised.components:
            prior = before.get(comp.id)
            if not prior or prior.model_dump() != comp.model_dump():
                changes.append({"component_id": comp.id, "before": prior.model_dump() if prior else None, "after": comp.model_dump()})
        chaos = self.chaos(chosen, req.deadline_days)
        readiness = self.readiness(chosen, chaos, req)
        commercial = self.commercial_viability(revised, req, chosen, budget)
        payload = {
            "project_id": uuid.uuid4().hex[:12], "parent_project_id": project_id, "created_at": now_iso(),
            "demand_key": old.get("demand_key") or project_demand_key(req.model_dump()),
            "repair": {"blocked_supplier": supplier, "mutation_count": len(changes), "changes": changes},
            "request": req.model_dump(), "design": revised.model_dump(), "graph": self.graph(revised), "evidence": evidence,
            "plans": plans, "chaos": chaos, "readiness": readiness, "economics": self.economics(chosen, req),
            "commercial": commercial, "fabrication": old.get("fabrication", {}),
            "operation_searches": {"live_searches": budget.used, "limit": budget.limit},
        }
        payload["serpapi_quota"] = self.quota()
        payload["certificate"] = self._certificate(payload)
        self.store.save_project(payload["project_id"], payload)
        return payload
    def scenario(self, project_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        """Run a no-search what-if scenario against an existing evidence snapshot."""
        old = self.store.get_project(project_id)
        blocked_supplier = str(raw.get("blocked_supplier") or "").strip()
        price_shock = max(0.0, min(200.0, float(raw.get("price_shock_percent") or 0)))
        budget_reduction = max(0.0, min(80.0, float(raw.get("budget_reduction_percent") or 0)))
        deadline_days = int(raw.get("deadline_days") or old["request"].get("deadline_days", 21))
        diversify = bool(raw.get("require_supplier_diversity", False))
        if not blocked_supplier and not price_shock and not budget_reduction and not diversify and deadline_days == old["request"].get("deadline_days", 21):
            raise ForgeError("Choose at least one stress condition before running the scenario", 422)
        request_data = dict(old["request"])
        request_data["budget"] = round(float(request_data["budget"]) * (1 - budget_reduction / 100), 2)
        request_data["deadline_days"] = max(1, min(365, deadline_days))
        req = CompileRequest.model_validate(request_data)
        design = Design.model_validate(old["design"])
        evidence = json.loads(json.dumps(old["evidence"]))
        if price_shock:
            multiplier = 1 + price_shock / 100
            for entry in evidence.values():
                for candidate in entry.get("candidates", []):
                    candidate["price"] = round(float(candidate["price"]) * multiplier, 2)
                    candidate["price_text"] = f"Scenario-adjusted +{price_shock:.0f}%"
        supplier_cap = max(1, math.ceil(len(design.components) / 2)) if diversify else None
        blocked = {blocked_supplier} if blocked_supplier else set()
        plans = [self.optimize(design, evidence, req, strategy, blocked, supplier_cap) for strategy in ("cheapest", "balanced", "resilient")]
        chosen = next((p for p in plans if p.get("strategy") == "balanced" and p.get("status") in {"optimal", "feasible"}),
                      next((p for p in plans if p.get("status") in {"optimal", "feasible"}), plans[0]))
        chaos = self.chaos(chosen, req.deadline_days)
        readiness = self.readiness(chosen, chaos, req)
        baseline = next((p for p in old["plans"] if p.get("strategy") == "balanced" and p.get("status") in {"optimal", "feasible"}),
                        next((p for p in old["plans"] if p.get("status") in {"optimal", "feasible"}), old["plans"][0]))
        baseline_items = {x["component_id"]: x for x in baseline.get("selections", [])}
        revised_items = {x["component_id"]: x for x in chosen.get("selections", [])}
        changes = []
        for component_id in sorted(set(baseline_items) | set(revised_items)):
            before, after = baseline_items.get(component_id), revised_items.get(component_id)
            if (before or {}).get("id") != (after or {}).get("id"):
                changes.append({
                    "component_id": component_id,
                    "component_name": (after or before or {}).get("component_name", component_id),
                    "before": {"product": before.get("title"), "supplier": before.get("supplier"), "cost": before.get("line_cost")} if before else None,
                    "after": {"product": after.get("title"), "supplier": after.get("supplier"), "cost": after.get("line_cost")} if after else None,
                })
        scenario_info = {
            "blocked_supplier": blocked_supplier or None,
            "price_shock_percent": price_shock,
            "budget_reduction_percent": budget_reduction,
            "deadline_days": req.deadline_days,
            "require_supplier_diversity": diversify,
            "uses_new_searches": False,
        }
        comparison = {
            "baseline_project_id": project_id,
            "baseline_cost": baseline.get("total_cost"),
            "revised_cost": chosen.get("total_cost") if chosen.get("status") in {"optimal", "feasible"} else None,
            "cost_change": round(float(chosen.get("total_cost", 0)) - float(baseline.get("total_cost", 0)), 2) if chosen.get("total_cost") is not None else None,
            "baseline_completion": old.get("chaos", {}).get("completion_probability"),
            "revised_completion": chaos.get("completion_probability"),
            "baseline_readiness": old.get("readiness", {}).get("score"),
            "revised_readiness": readiness.get("score"),
            "changed_components": changes,
            "feasible": chosen.get("status") in {"optimal", "feasible"},
        }
        payload = {
            "project_id": uuid.uuid4().hex[:12], "parent_project_id": project_id, "created_at": now_iso(),
            "demand_key": old.get("demand_key") or project_demand_key(old.get("request", {})),
            "scenario": scenario_info, "comparison": comparison, "request": req.model_dump(),
            "design": old["design"], "graph": old.get("graph", self.graph(design)), "evidence": evidence,
            "plans": plans, "chaos": chaos, "readiness": readiness, "economics": self.economics(chosen, req),
            "commercial": old.get("commercial", {}), "fabrication": old.get("fabrication", {}),
            "operation_searches": {"live_searches": 0, "limit": self.serp_operation_limit},
            "serpapi_quota": self.quota(),
        }
        payload["certificate"] = self._certificate(payload)
        self.store.save_project(payload["project_id"], payload)
        return payload
