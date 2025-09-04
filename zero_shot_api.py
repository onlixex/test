#!/usr/bin/env python3
# zero_shot_api.py
import os
import json
import logging
from typing import List, Optional
import requests
import yaml

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from transformers import pipeline

# -----------------------
# Конфигурация (env)
# -----------------------
ZAMMAD_URL = os.getenv("ZAMMAD_URL", "http://127.0.0.1:3000")
ZAMMAD_TOKEN = os.getenv("ZAMMAD_TOKEN", "PASTE_ZAMMAD_TOKEN")
MODEL_NAME = os.getenv("ZS_MODEL", "joeddav/xlm-roberta-large-xnli")
CASES_PATH = os.getenv("CASES_PATH", "cases.yaml")
APPLY_TAGS = os.getenv("APPLY_TAGS", "false").lower() in ("1", "true", "yes")
DOMAIN_THRESHOLD = float(os.getenv("DOMAIN_THRESHOLD", "0.65"))
ACTION_THRESHOLD = float(os.getenv("ACTION_THRESHOLD", "0.6"))
CASE_THRESHOLD = float(os.getenv("CASE_THRESHOLD", "0.55"))

# -----------------------
# Логирование
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zero_shot")

# -----------------------
# FastAPI
# -----------------------
app = FastAPI(title="Zero-shot classifier with cases", version="1.0")

# -----------------------
# Модель
# -----------------------
try:
    classifier = pipeline("zero-shot-classification", model=MODEL_NAME)
    log.info(f"Model loaded: {MODEL_NAME}")
except Exception as e:
    log.exception("Model load failed")
    classifier = None

# -----------------------
# Загрузка справочника cases.yaml
# -----------------------
def load_cases(path: str):
    step = "load_cases"
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        domains = doc.get("domains", {})
        if not isinstance(domains, dict) or not domains:
            raise ValueError("Field 'domains' missing or empty in cases.yaml")
        # normalize nothing yet — keep structure
        log.info(f"[{step}] Loaded domains: {list(domains.keys())}")
        return domains
    except Exception as e:
        log.exception(f"[{step}] Error loading cases.yaml: {e}")
        raise RuntimeError(f"{step}: {e}")

try:
    CASES = load_cases(CASES_PATH)
except Exception as e:
    CASES = {}
    log.error("Cases not loaded; endpoint will report error on classify")

# -----------------------
# Zammad helpers
# -----------------------
def zammad_headers():
    return {"Authorization": f"Token token={ZAMMAD_TOKEN}"}

def fetch_tag_list(step_label="fetch_tag_list") -> List[str]:
    try:
        url = f"{ZAMMAD_URL}/api/v1/tag_list"
        r = requests.get(url, headers=zammad_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        # data is list of dicts {id,name,count}
        tags = [it["name"] for it in data if isinstance(it, dict) and "name" in it]
        log.info(f"[{step_label}] fetched {len(tags)} tags from Zammad")
        return tags
    except Exception as e:
        log.exception(f"[{step_label}] Failed to fetch tag_list from Zammad: {e}")
        raise RuntimeError(f"{step_label}: {e}")

def add_tag_to_ticket(ticket_id: int, tag: str, step_label="add_tag"):
    try:
        url = f"{ZAMMAD_URL}/api/v1/tags/add"
        payload = {"object": "Ticket", "o_id": ticket_id, "name": tag}
        r = requests.post(url, headers=zammad_headers(), json=payload, timeout=10)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Zammad tags/add responded {r.status_code}: {r.text}")
        log.info(f"[{step_label}] Added tag '{tag}' to ticket {ticket_id}")
        return True
    except Exception as e:
        log.exception(f"[{step_label}] Error adding tag: {e}")
        raise RuntimeError(f"{step_label}: {e}")

# -----------------------
# Utility
# -----------------------
def normalize(s: str) -> str:
    return s.strip().lower().replace(" ", "_")

def choose_tag_from_available(desired_candidates: List[str], available_tags: List[str]) -> Optional[str]:
    # try exact, then normalized match, then substring
    for c in desired_candidates:
        if c in available_tags:
            return c
    norm_avail = {normalize(t): t for t in available_tags}
    for c in desired_candidates:
        nc = normalize(c)
        if nc in norm_avail:
            return norm_avail[nc]
    # fallback: try contains
    for a in available_tags:
        for c in desired_candidates:
            if normalize(c) in normalize(a) or normalize(a) in normalize(c):
                return a
    return None

# -----------------------
# Request/Response models
# -----------------------
class ClassifyRequest(BaseModel):
    text: str
    ticket_id: Optional[int] = None   # optional: if present and APPLY_TAGS true -> auto apply

# -----------------------
# Endpoints
# -----------------------
@app.get("/cases/")
def api_cases():
    if not CASES:
        return JSONResponse(status_code=500, content={"step":"load_cases","error":"cases not loaded"})
    return JSONResponse(content=CASES)

@app.post("/classify/")
def api_classify(req: ClassifyRequest):
    step = "start"
    try:
        if classifier is None:
            raise RuntimeError("Model not loaded")

        if not CASES:
            raise RuntimeError("Cases data is empty (cases.yaml)")

        # 1) prepare candidate domain labels
        step = "prepare_domains"
        domains = list(CASES.keys())
        if not domains:
            raise RuntimeError("No domains in cases.yaml")

        # 2) domain classification
        step = "classify_domain"
        dom_res = classifier(req.text, candidate_labels=domains, multi_label=False)
        dom_label = dom_res["labels"][0]
        dom_score = float(dom_res["scores"][0])
        log.info(f"[{step}] domain='{dom_label}' score={dom_score}")

        # 3) prepare actions for predicted domain
        step = "prepare_actions"
        actions_dict = CASES.get(dom_label) or CASES.get(dom_label.strip()) or {}
        actions = list(actions_dict.keys())
        if not actions:
            raise RuntimeError(f"No actions found for domain '{dom_label}'")

        # 4) action classification
        step = "classify_action"
        act_res = classifier(req.text, candidate_labels=actions, multi_label=False)
        act_label = act_res["labels"][0]
        act_score = float(act_res["scores"][0])
        log.info(f"[{step}] action='{act_label}' score={act_score}")

        # 5) case selection among actions[act_label] (if present)
        step = "classify_case"
        cases_for_action = actions_dict.get(act_label, []) or []
        case_label = None
        case_score = None
        if cases_for_action:
            case_res = classifier(req.text, candidate_labels=cases_for_action, multi_label=False)
            case_label = case_res["labels"][0]
            case_score = float(case_res["scores"][0])
            log.info(f"[{step}] case='{case_label}' score={case_score}")

        # 6) determine need_confirmation
        step = "decide_confirmation"
        need_confirm = (dom_score < DOMAIN_THRESHOLD) or (act_score < ACTION_THRESHOLD) or (case_label and case_score < CASE_THRESHOLD)
        log.info(f"[{step}] need_confirmation={need_confirm}")

        # 7) fetch available tags from Zammad to map label -> tag
        step = "fetch_tags"
        try:
            available_tags = fetch_tag_list(step_label=step)
        except Exception as e:
            # don't fail whole flow — just log and continue with empty available_tags
            available_tags = []
            log.warning(f"[{step}] could not fetch tags: {e}")

        # prepare candidates for mapping (prefer forms)
        candidates_domain = [dom_label, dom_label.lower(), dom_label.replace(" ", "_")]
        candidates_action = [act_label, act_label.lower(), act_label.replace(" ", "_")]
        mapped_domain_tag = choose_tag_from_available(candidates_domain, available_tags) if available_tags else None
        mapped_action_tag = choose_tag_from_available(candidates_action, available_tags) if available_tags else None

        # If nothing mapped, still prepare canonical names (normalized)
        canonical_domain = dom_label
        canonical_action = act_label

        # 8) optionally apply tags to ticket
        applied = []
        if APPLY_TAGS and req.ticket_id and not need_confirm:
            step = "apply_tags"
            if mapped_domain_tag:
                add_tag_to_ticket(req.ticket_id, mapped_domain_tag, step_label=step)
                applied.append(mapped_domain_tag)
            else:
                # try to apply canonical domain name as tag
                try:
                    add_tag_to_ticket(req.ticket_id, canonical_domain)
                    applied.append(canonical_domain)
                except Exception as e:
                    log.warning(f"[{step}] could not add domain tag {canonical_domain}: {e}")

            if mapped_action_tag:
                add_tag_to_ticket(req.ticket_id, mapped_action_tag, step_label=step)
                applied.append(mapped_action_tag)
            else:
                try:
                    add_tag_to_ticket(req.ticket_id, canonical_action)
                    applied.append(canonical_action)
                except Exception as e:
                    log.warning(f"[{step}] could not add action tag {canonical_action}: {e}")

        # 9) Build response
        result = {
            "input": req.text,
            "domain": {"label": dom_label, "score": round(dom_score, 4), "mapped_tag": mapped_domain_tag},
            "action": {"label": act_label, "score": round(act_score, 4), "mapped_tag": mapped_action_tag},
            "case": {"label": case_label, "score": round(case_score, 4)} if case_label else None,
            "need_confirmation": need_confirm,
            "available_tags_count": len(available_tags),
            "applied_tags": applied if applied else None
        }

        # Возвращаем с корректной кодировкой
        return JSONResponse(content=json.loads(json.dumps(result, ensure_ascii=False)))

    except Exception as e:
        log.exception(f"Error at step '{step}': {e}")
        # возвращаем понятный ответ с шагом, где упало
        return JSONResponse(status_code=500, content={"step": step, "error": str(e)})
