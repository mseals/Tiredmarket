"""Tired Market — canonical model registry (v4.14.0).

The registry is the translation table between each provider's
model string and a provider-neutral canonical model id. It exists
because Groq calling its Llama 3.3 70B `llama-3.3-70b-versatile`,
Cerebras calling the same model `llama3.3-70b`, and Sambanova
calling it `Meta-Llama-3.3-70B-Instruct` is the same model — and
the router needs to know that so it can:

  - dedupe consensus votes (one canonical model = one vote, no
    matter how many providers serve it),
  - fail over between providers serving the same canonical model
    when one trips a quota/cooldown.

Two files back the registry:

  - data/model_registry.default.json  — bundled, ships with the app,
    NEVER edited by user or app at runtime. Updates ship via app
    upgrades.

  - data/model_registry.json          — optional user override.
    Created lazily by discovery code (v4.14.0 stage 7) when it
    learns of a new provider→model mapping, or by a power user
    editing a mapping by hand. Survives app upgrades.

The loader merges override on top of default at load time so
callers see one resolved view. Merge semantics per canonical_id:

  - display_name: override wins, default fallback
  - class:        override wins, default fallback
  - provider_strings: dict-merge (override entries replace default
    for the same provider_id; default entries kept where override
    doesn't specify)
  - new canonical_ids in override:  added wholesale
  - canonical_ids only in default:  kept wholesale

Discovery code uses `add_provider_mapping(...)` to persist a new
mapping to the override file without touching the default.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


# Default values used when a record is missing the field. Keeps the
# loader tolerant of partial entries (e.g. a discovery-written entry
# that only knows the canonical_id and one provider mapping).
_DEFAULT_CLASS: Optional[str] = None
_VALID_CLASSES = ("A", "B")


class ModelRegistry:
    """Canonical model registry. Thread-safe; reload-on-demand.

    Typical usage:
        reg = ModelRegistry(DATA_DIR)
        cid = reg.get_canonical("groq", "llama-3.3-70b-versatile")
        # cid == "meta/llama-3.3-70b-instruct"
        provs = reg.get_provider_strings(cid)
        # provs == {"groq": "llama-3.3-70b-versatile",
        #           "cerebras": "llama3.3-70b",
        #           "sambanova": "Meta-Llama-3.3-70B-Instruct"}
    """

    DEFAULT_FILENAME = "model_registry.default.json"
    OVERRIDE_FILENAME = "model_registry.json"

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.default_path = self.data_dir / self.DEFAULT_FILENAME
        self.override_path = self.data_dir / self.OVERRIDE_FILENAME
        self._lock = threading.Lock()
        self._merged: dict[str, dict] = {}
        # (provider_id, provider_string_lower) -> canonical_id
        self._provider_index: dict[tuple[str, str], str] = {}
        self.reload()

    # ── load / merge ────────────────────────────────────────────

    def reload(self) -> None:
        """Re-read both files and rebuild the merged view + provider
        index. Safe to call any time. Tolerant of either file being
        missing or malformed (logs and falls back to whatever it
        could parse)."""
        default = self._read_one(self.default_path, required=True)
        override = self._read_one(self.override_path, required=False)

        merged: dict[str, dict] = {}

        for entry in default.get("models", []):
            cid = entry.get("canonical_id")
            if not cid:
                continue
            merged[cid] = self._normalize_entry(entry)

        for entry in override.get("models", []):
            cid = entry.get("canonical_id")
            if not cid:
                continue
            base = merged.get(cid)
            merged[cid] = self._merge_entry(base, entry)

        index: dict[tuple[str, str], str] = {}
        for cid, entry in merged.items():
            for prov, pstring in entry.get("provider_strings", {}).items():
                if not isinstance(pstring, str) or not pstring:
                    continue
                key = (prov, pstring.lower())
                # First-write-wins on lookup conflict (shouldn't
                # happen in practice; if it does, the registry needs
                # a human-readable warning here later).
                index.setdefault(key, cid)

        with self._lock:
            self._merged = merged
            self._provider_index = index

    @staticmethod
    def _read_one(path: Path, required: bool) -> dict:
        if not path.exists():
            if required:
                # Missing default is a programming error (the file
                # ships with the install). Return empty rather than
                # crash, but a future caller may want a louder
                # signal.
                return {"version": 1, "models": []}
            return {"version": 1, "models": []}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"version": 1, "models": []}
            if not isinstance(data.get("models"), list):
                data["models"] = []
            return data
        except Exception:
            return {"version": 1, "models": []}

    @staticmethod
    def _normalize_entry(entry: dict) -> dict:
        cid = entry.get("canonical_id")
        provs = entry.get("provider_strings") or {}
        if not isinstance(provs, dict):
            provs = {}
        cls = entry.get("class")
        if cls not in _VALID_CLASSES:
            cls = _DEFAULT_CLASS
        return {
            "canonical_id": cid,
            "display_name": entry.get("display_name") or cid,
            "class": cls,
            "provider_strings": dict(provs),
        }

    @staticmethod
    def _merge_entry(base: Optional[dict], override: dict) -> dict:
        norm = ModelRegistry._normalize_entry(override)
        if base is None:
            return norm
        merged_provs = dict(base.get("provider_strings", {}))
        merged_provs.update(norm["provider_strings"])
        return {
            "canonical_id": norm["canonical_id"],
            "display_name": norm["display_name"]
                            if override.get("display_name")
                            else base.get("display_name"),
            "class": norm["class"]
                     if override.get("class") in _VALID_CLASSES
                     else base.get("class"),
            "provider_strings": merged_provs,
        }

    # ── read API ────────────────────────────────────────────────

    def get_canonical(self, provider_id: str,
                      provider_model_string: str) -> Optional[str]:
        """Look up the canonical model id for a (provider, model
        string) pair. Returns None if not in the registry — caller
        can then log it as 'unknown' (per the design doc) without
        crashing."""
        if not provider_id or not provider_model_string:
            return None
        key = (provider_id, provider_model_string.lower())
        with self._lock:
            return self._provider_index.get(key)

    def get_provider_strings(self, canonical_id: str) -> dict[str, str]:
        """Return {provider_id: that-provider's-model-string} for a
        canonical id. Empty dict if unknown."""
        with self._lock:
            entry = self._merged.get(canonical_id)
            if not entry:
                return {}
            return dict(entry.get("provider_strings", {}))

    def get_class(self, canonical_id: str) -> Optional[str]:
        """Class A (consensus voting) or Class B (utility tasks).
        None if unknown or unset."""
        with self._lock:
            entry = self._merged.get(canonical_id)
            if not entry:
                return None
            return entry.get("class")

    def get_display_name(self, canonical_id: str) -> Optional[str]:
        with self._lock:
            entry = self._merged.get(canonical_id)
            if not entry:
                return None
            return entry.get("display_name") or canonical_id

    def list_canonical_ids(self) -> list[str]:
        """All canonical model ids known to the registry, sorted."""
        with self._lock:
            return sorted(self._merged.keys())

    def list_by_class(self, cls: str) -> list[str]:
        """All canonical model ids with the given class ('A' or 'B'),
        sorted."""
        with self._lock:
            return sorted(cid for cid, e in self._merged.items()
                          if e.get("class") == cls)

    def list_providers_for_class(self, cls: str) -> dict[str, list[str]]:
        """For a class ('A' or 'B'), return a dict of
        {canonical_id: [provider_ids serving it]} — the data shape
        the router needs to build model groups."""
        with self._lock:
            out: dict[str, list[str]] = {}
            for cid, entry in self._merged.items():
                if entry.get("class") != cls:
                    continue
                out[cid] = sorted(entry.get("provider_strings", {}).keys())
            return out

    # ── write API (override file only) ─────────────────────────

    def add_provider_mapping(self, canonical_id: str, provider_id: str,
                              provider_model_string: str,
                              display_name: Optional[str] = None,
                              cls: Optional[str] = None) -> None:
        """Persist a new (provider_id → provider_model_string)
        mapping under the given canonical_id to the user override
        file. Used by discovery code (v4.14.0 stage 7) when it
        learns of a new mapping from a provider's `/v1/models`
        endpoint, or by manual edit code in Advanced UI mode.

        NEVER touches the bundled default file. Creates the override
        file if it doesn't exist. If the canonical_id already exists
        in the override, the new provider mapping is added/updated;
        existing provider mappings are preserved.
        """
        if not canonical_id or not provider_id or not provider_model_string:
            raise ValueError("canonical_id, provider_id, and "
                             "provider_model_string are all required")
        if cls is not None and cls not in _VALID_CLASSES:
            raise ValueError(
                f"cls must be one of {_VALID_CLASSES} or None")

        with self._lock:
            existing = self._read_one(self.override_path, required=False)
            models = existing.get("models")
            if not isinstance(models, list):
                models = []

            # Find the entry under this canonical_id, if any.
            entry = None
            for m in models:
                if m.get("canonical_id") == canonical_id:
                    entry = m
                    break

            if entry is None:
                entry = {"canonical_id": canonical_id,
                         "provider_strings": {}}
                models.append(entry)

            ps = entry.setdefault("provider_strings", {})
            ps[provider_id] = provider_model_string

            if display_name:
                entry["display_name"] = display_name
            if cls is not None:
                entry["class"] = cls

            existing.setdefault("version", 1)
            existing["models"] = models
            existing["_last_updated"] = datetime.now().isoformat()
            existing.setdefault(
                "_comment",
                "User override for the model registry. Edit this "
                "file (or let the app's discovery code edit it) to "
                "add or change provider→model mappings without "
                "touching the bundled default. The app merges this "
                "on top of model_registry.default.json at load time."
            )

            self._write_override(existing)

        # Re-load so in-memory state matches disk.
        self.reload()

    def _write_override(self, data: dict) -> None:
        """Atomic-ish write: temp file then rename. Keeps the
        override file from getting half-written if the process
        crashes mid-save."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.override_path.with_suffix(self.override_path.suffix
                                              + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        tmp.replace(self.override_path)
