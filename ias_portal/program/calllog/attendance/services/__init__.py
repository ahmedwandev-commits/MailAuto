"""Use cases. They talk to storage ONLY through ``Repository`` and to the rules through ``domain``."""
import hashlib
import json


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def audit(repo, user, action, entity=None, entity_id=None, detail=None, ip=None):
    repo.audit({"user_id": (user or {}).get("user_id"), "username": (user or {}).get("name"), "action": action,
                "entity": entity, "entity_id": entity_id, "detail": detail or {}, "ip": ip})
