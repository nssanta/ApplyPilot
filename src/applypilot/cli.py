from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import random
import sys
import time
import uuid
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__, letters
from .analytics import report
from .config import (
    AppConfig,
    ConfigError,
    aitunnel_api_key,
    effective_search,
    ensure_data_dirs,
    search_groups,
    search_origins,
    validate_search,
)
from .cover_letters import letter_mode, load_letter_profile, render_template
from .llm import generate
from .pacing import next_delay
from .parser import (
    ScanSegment,
    enrich_items,
    load_items,
    save_snapshot,
    scan_many,
    stamp_first_seen,
)
from .presets import ROLE_PRESETS
from .quality import run_benchmark
from .review import write_review
from .scoring import (
    choose_resume,
    evaluate_search_filter,
    filter_candidates,
    prioritize_for_enrichment,
)
from .screen import (
    DEFAULT_BASE_URL,
    DEFAULT_CONCURRENCY,
    DEFAULT_MODEL,
    ScreenError,
    screen_vacancies,
)
from .session import check_session, login, save_state, validate_state
from .storage import Store
from .templates import create_template, list_templates

PRESET_CHOICES = tuple(ROLE_PRESETS)
LOGGER = logging.getLogger(__name__)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", help="каталог рабочих данных")
    parser.add_argument("--profile", help="приватный TOML-профиль")
    parser.add_argument("--search", help="TOML-конфигурация поиска")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="applypilot", description="Локальный процесс поиска вакансий и откликов на HH.ru")
    parser.add_argument("--version", action="version", version=__version__)
    _common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="проверить локальную установку")
    doctor.add_argument("--online", action="store_true", help="выполнить небольшой публичный сетевой тест")

    sub.add_parser("login", help="открыть изолированный браузер для ручного входа")
    session = sub.add_parser("session", help="проверить сохранённую сессию")
    session.add_argument("action", choices=["check"])

    scan_cmd = sub.add_parser("scan", help="получить снимок вакансий")
    scan_cmd.add_argument("--query")
    scan_cmd.add_argument("--add-query", action="append",
                          help="добавить запрос, не заменяя запросы пресета")
    scan_cmd.add_argument("--area", type=int, action="append")
    scan_cmd.add_argument("--page", type=int)
    scan_cmd.add_argument("--pages", type=int)
    scan_cmd.add_argument("--request-budget", type=int,
                          help="максимум поисковых HTTP-запросов; имеет приоритет над TOML")
    scan_cmd.add_argument(
        "--sort-mode",
        choices=("relevance", "newest", "balanced"),
        help="стратегия сортировки; по умолчанию из TOML (исторически relevance)",
    )
    scan_cmd.add_argument("--days", type=int)
    scan_cmd.add_argument("--remote", action=argparse.BooleanOptionalAction, default=None)
    scan_cmd.add_argument("--preset", choices=PRESET_CHOICES)
    scan_cmd.add_argument("--details-limit", type=int)
    scan_cmd.add_argument("--salary-from", type=int)
    scan_cmd.add_argument("--salary-currency")
    scan_cmd.add_argument("--salary-missing", choices=("include", "exclude", "only"))
    scan_cmd.add_argument("--experience", action="append")
    scan_cmd.add_argument("--work-format", action="append")

    run_cmd = sub.add_parser("run", help="запустить ручную операцию")
    run_sub = run_cmd.add_subparsers(dest="run_action", required=True)
    campaign = run_sub.add_parser("campaign", help="выполнить HH scan и сохранить очередь вакансий")
    campaign.add_argument("--track", required=True, help="ключ трека из private/config/tracks.toml")
    campaign.add_argument("--query", action="append", help="поисковая фраза; можно указать несколько раз")
    campaign.add_argument("--area", type=int, action="append", help="регион HH; можно указать несколько раз")
    campaign.add_argument("--days", type=int, default=3)
    campaign.add_argument("--pages", type=int)
    campaign.add_argument("--request-budget", type=int)
    campaign.add_argument("--details-limit", type=int)
    campaign.add_argument("--limit", type=int, default=100, help="верхняя граница очереди")
    campaign.add_argument("--remote", action=argparse.BooleanOptionalAction, default=None)
    campaign.add_argument("--salary-from", type=int)
    campaign.add_argument("--salary-currency")
    campaign.add_argument("--sort-mode", choices=("newest", "relevance", "balanced"), default="newest")
    campaign.add_argument("--preset", choices=PRESET_CHOICES)

    bundle = run_sub.add_parser("bundle", help="собрать до 100 кандидатов из нескольких треков")
    bundle.add_argument("--track", action="append", choices=("ai", "backend", "ml"),
                        help="направление, повторить при необходимости; default: все три")
    bundle.add_argument("--days", type=int, default=3)
    bundle.add_argument("--pages", type=int, default=1)
    bundle.add_argument("--request-budget", type=int, default=90,
                        help="число поисковых HTTP-запросов на каждый трек")
    bundle.add_argument("--details-limit", type=int, default=90,
                        help="число полных описаний на каждый трек")
    bundle.add_argument("--limit", type=int, default=100)
    bundle.add_argument("--remote", action=argparse.BooleanOptionalAction, default=True)
    bundle.add_argument("--salary-from", type=int, default=140000)
    bundle.add_argument("--sort-mode", choices=("newest", "relevance", "balanced"),
                        default="newest")
    bundle.add_argument("--wide-ai", action=argparse.BooleanOptionalAction, default=True,
                        help="использовать private/config/search-ai-broad.toml, если он есть")

    queue = sub.add_parser("queue", help="постоянная очередь вакансий")
    queue_sub = queue.add_subparsers(dest="queue_action", required=True)
    queue_list = queue_sub.add_parser("list", help="показать вакансии из SQLite")
    queue_list.add_argument("--track")
    queue_list.add_argument("--source", help="фильтр по источнику; без флага — все источники")
    queue_list.add_argument("--view", choices=(
        "all", "new", "top", "deferred", "approved", "ready", "needs_letter", "needs_form",
        "external_ats", "unknown", "applied", "skip",
    ), default="all")
    queue_list.add_argument("--limit", type=int, default=100)
    queue_list.add_argument("--json", action="store_true")
    queue_set = queue_sub.add_parser("set", help="сохранить решение владельца по вакансии")
    queue_set.add_argument("--track", required=True)
    queue_set.add_argument("--source", default="hh.ru", help="источник вакансии")
    queue_set.add_argument("--id", required=True)
    queue_set.add_argument("--state", required=True,
                           choices=("FIT", "MAYBE", "SKIP", "APPROVED", "DEFERRED", "UNREVIEWED"))
    queue_set.add_argument("--reason", default="")
    queue_set.add_argument("--notes")
    queue_batch = queue_sub.add_parser("batch-set", help="атомарно пометить пакет вакансий")
    queue_batch.add_argument("--track", required=True)
    queue_batch.add_argument("--source", default="hh.ru", help="источник вакансий")
    queue_batch.add_argument("--id", action="append", required=True,
                             help="ID вакансии; повторить до 100 раз")
    queue_batch.add_argument("--state", required=True,
                             choices=("FIT", "MAYBE", "SKIP", "APPROVED", "DEFERRED", "UNREVIEWED"))
    queue_batch.add_argument("--reason", default="")
    queue_history = queue_sub.add_parser("history", help="история запусков поиска")
    queue_history.add_argument("--track")
    queue_history.add_argument("--limit", type=int, default=20)
    queue_history.add_argument("--json", action="store_true")
    queue_apply = queue_sub.add_parser("apply", help="dry-run или явный запуск по APPROVED из очереди")
    queue_apply.add_argument("--track", required=True)
    queue_apply.add_argument("--source", default="hh.ru", help="источник очереди")
    queue_apply.add_argument("--limit", type=int, default=100)
    apply_mode = queue_apply.add_mutually_exclusive_group(required=True)
    apply_mode.add_argument("--dry-run", action="store_true")
    apply_mode.add_argument("--run", action="store_true")
    queue_apply.add_argument("--target-success", type=int)
    queue_mode = queue_apply.add_mutually_exclusive_group()
    queue_mode.add_argument("--visible", dest="visible", action="store_true",
                            help="открыть реальное окно Chrome (ручная CAPTCHA)")
    queue_mode.add_argument("--headless", dest="headless", action="store_true",
                            help="без окна; CAPTCHA нельзя пройти вручную")

    sources = sub.add_parser("sources", help="мультиплатформенные источники и их сессии")
    sources_sub = sources.add_subparsers(dest="sources_action", required=True)
    sources_list = sources_sub.add_parser("list", help="показать источники и их возможности")
    sources_list.add_argument("--json", action="store_true")
    sources_status = sources_sub.add_parser("status", help="показать очередь и состояние источников")
    sources_status.add_argument("--track", required=True)
    sources_status.add_argument("--json", action="store_true")
    for action_name, action_help in (
        ("login", "открыть видимое окно ручной авторизации источника"),
        ("check", "проверить сохранённую сессию источника"),
    ):
        action = sources_sub.add_parser(action_name, help=action_help)
        action.add_argument("--source", required=True)
        action.add_argument("--account", default=None, help="по умолчанию из sources.toml")
        action.add_argument("--wait-seconds", type=int, default=300,
                            help="сколько ждать подтверждения входа; 0 — не ждать")
        action.add_argument("--no-interactive", action="store_true",
                            help="не читать stdin; только окно браузера и проверка маркера")
        action.add_argument("--json", action="store_true")
    sources_form_audit = sources_sub.add_parser(
        "form-audit", help="проверить реальные формы вакансий без нажатий и отправок",
    )
    sources_form_audit.add_argument("--track", required=True)
    sources_form_audit.add_argument("--source", required=True)
    sources_form_audit.add_argument(
        "--view", choices=("top", "review", "approved", "all"), default="review",
    )
    sources_form_audit.add_argument("--limit", type=int, default=100)
    sources_form_audit.add_argument("--delay-seconds", type=float, default=0.75)
    sources_form_audit.add_argument("--json", action="store_true")
    sources_scan = sources_sub.add_parser("scan", help="сканировать все или выбранные источники")
    sources_scan.add_argument("--track", required=True)
    sources_scan.add_argument("--source", action="append", help="повторить для выбора нескольких")
    sources_scan.add_argument("--pages", type=int)
    sources_scan.add_argument("--request-budget", type=int)
    sources_scan.add_argument("--details-limit", type=int)
    sources_scan.add_argument("--limit", type=int, default=100)
    sources_scan.add_argument("--fixture-dir", help="офлайн-каталог публичных HTML-образцов")
    sources_scan.add_argument(
        "--telegram-hints", action="store_true",
        help="Getmatch: обогатить вакансии из уже сохранённых сообщений @g_jobbot",
    )
    sources_scan.add_argument("--json", action="store_true")
    sources_approve = sources_sub.add_parser("approve", help="source-scoped решение по вакансиям")
    sources_approve.add_argument("--track", required=True)
    sources_approve.add_argument("--source", required=True)
    sources_approve.add_argument("--id", action="append", required=True)
    sources_approve.add_argument("--state", default="APPROVED",
                                 choices=("FIT", "MAYBE", "SKIP", "APPROVED", "DEFERRED", "UNREVIEWED"))
    sources_approve.add_argument("--reason", default="")
    sources_plan = sources_sub.add_parser("plan", help="dry-run план очереди без отправки")
    sources_plan.add_argument("--track", required=True)
    sources_plan.add_argument("--source", required=True)
    sources_plan.add_argument("--limit", type=int, default=100)
    sources_plan.add_argument("--json", action="store_true")

    sources_dispatch_plan = sources_sub.add_parser(
        "dispatch-plan", help="общий dry-run план по всем или выбранным источникам",
    )
    sources_dispatch_plan.add_argument("--track", required=True)
    sources_dispatch_plan.add_argument("--source", action="append")
    sources_dispatch_plan.add_argument("--limit", type=int, default=100)
    sources_dispatch_plan.add_argument("--json", action="store_true")

    sources_dispatch_apply = sources_sub.add_parser(
        "dispatch-apply", help="общий диспетчер откликов; по умолчанию только dry-run",
    )
    sources_dispatch_apply.add_argument("--track", required=True)
    sources_dispatch_apply.add_argument("--source", action="append")
    sources_dispatch_apply.add_argument("--limit", type=int, default=100)
    sources_dispatch_apply.add_argument("--allow-live", action="store_true",
                                        help="разрешить live-драйверы; без драйвера источник пропускается")
    sources_dispatch_apply.add_argument(
        "--enable-habr", action="store_true",
        help="включить проверенный нативный Habr-драйвер (требует --allow-live и APPROVED)",
    )
    sources_dispatch_apply.add_argument(
        "--visible", action="store_true",
        help="Habr: использовать видимое окно Chrome для ручной проверки и CAPTCHA",
    )
    sources_dispatch_apply.add_argument(
        "--habr-min-interval", type=float, default=2.0,
        help="Habr: минимальные секунды между отправками в одной сессии (0..30)",
    )
    sources_dispatch_apply.add_argument("--json", action="store_true")

    sources_approve_batch = sources_sub.add_parser(
        "approve-batch", help="атомарное пакетное решение по вакансиям источника",
    )
    sources_approve_batch.add_argument("--track", required=True)
    sources_approve_batch.add_argument("--source", required=True)
    sources_approve_batch.add_argument("--id", action="append",
                                       help="ID или список ID через запятую; повторите флаг")
    sources_approve_batch.add_argument("--from-view", choices=("top", "review", "all"))
    sources_approve_batch.add_argument("--limit", type=int, default=100)
    sources_approve_batch.add_argument(
        "--state", default="APPROVED",
        choices=("FIT", "MAYBE", "SKIP", "APPROVED", "DEFERRED", "UNREVIEWED"),
    )
    sources_approve_batch.add_argument("--reason", default="")
    sources_approve_batch.add_argument("--json", action="store_true")

    sources_accounts = sources_sub.add_parser("accounts", help="показать source account mapping")
    sources_accounts.add_argument("--track", required=True)
    sources_accounts.add_argument("--json", action="store_true")
    sources_recover = sources_sub.add_parser("recover", help="карантинить прерванные submitting-попытки")
    sources_recover.add_argument("--account")
    sources_recover.add_argument("--max-age-seconds", type=int, default=900)
    sources_recover.add_argument("--json", action="store_true")
    sources_reconcile = sources_sub.add_parser("reconcile", help="подтвердить unknown только с evidence")
    sources_reconcile.add_argument("--track", required=True)
    sources_reconcile.add_argument("--source", required=True)
    sources_reconcile.add_argument("--id", required=True)
    sources_reconcile.add_argument("--channel", required=True)
    sources_reconcile.add_argument("--evidence", required=True)
    sources_reconcile.add_argument("--note", default="")
    sources_attempts = sources_sub.add_parser("attempts", help="история source-aware попыток")
    sources_attempts.add_argument("--source")
    sources_attempts.add_argument("--limit", type=int, default=200)
    sources_attempts.add_argument("--json", action="store_true")

    accounts = sub.add_parser("accounts", help="локальные аккаунты Telegram MTProto и HH OAuth")
    accounts_sub = accounts.add_subparsers(dest="accounts_action", required=True)

    accounts_list = accounts_sub.add_parser("list", help="показать локальные аккаунты и статусы")
    accounts_list.add_argument("--json", action="store_true")

    accounts_check = accounts_sub.add_parser("check", help="проверить статус; --online включает сеть")
    accounts_check.add_argument("--account", default=None)
    accounts_check.add_argument("--online", action="store_true")
    accounts_check.add_argument("--json", action="store_true")

    tg_setup = accounts_sub.add_parser("telegram-setup", help="сохранить API_ID/API_HASH локально")
    tg_setup.add_argument("--api-id", type=int)
    tg_setup.add_argument("--account", default=None)
    tg_setup.add_argument("--json", action="store_true")

    tg_login = accounts_sub.add_parser("telegram-login", help="локальный вход в Telegram")
    tg_login.add_argument("--account", default=None)
    tg_login.add_argument("--phone", default=None)
    tg_login.add_argument("--json", action="store_true")

    tg_status = accounts_sub.add_parser("telegram-status", help="статус Telegram-сессии")
    tg_status.add_argument("--account", default=None)
    tg_status.add_argument("--online", action="store_true")
    tg_status.add_argument("--json", action="store_true")

    tg_history = accounts_sub.add_parser("telegram-history", help="прочитать историю @g_jobbot")
    tg_history.add_argument("--account", default=None)
    tg_history.add_argument("--limit", type=int, default=50)
    tg_history.add_argument("--json", action="store_true")

    tg_sync = accounts_sub.add_parser("telegram-sync", help="сохранить историю и построить dry-run план")
    tg_sync.add_argument("--account", default=None)
    tg_sync.add_argument("--track", default=None)
    tg_sync.add_argument("--limit", type=int, default=50)
    tg_sync.add_argument("--json", action="store_true")

    tg_plan = accounts_sub.add_parser("telegram-plan", help="dry-run план из сохранённых сообщений")
    tg_plan.add_argument("--account", default=None)
    tg_plan.add_argument("--track", default=None)
    tg_plan.add_argument("--limit", type=int, default=50)
    tg_plan.add_argument("--json", action="store_true")

    tg_import = accounts_sub.add_parser(
        "telegram-import", help="связать сохранённые сообщения @g_jobbot с очередью Getmatch",
    )
    tg_import.add_argument("--account", default=None)
    tg_import.add_argument("--track", default=None)
    tg_import.add_argument("--source", default="getmatch.ru")
    tg_import.add_argument("--limit", type=int, default=200)
    tg_import.add_argument("--create-missing", action="store_true",
                           help="создать review-only placeholder для ещё не скачанной вакансии")
    tg_import.add_argument("--json", action="store_true")

    hh_setup = accounts_sub.add_parser("hh-oauth-setup", help="сохранить client_id/secret HH OAuth")
    hh_setup.add_argument("--client-id")
    hh_setup.add_argument("--redirect-uri", default=None)
    hh_setup.add_argument("--json", action="store_true")

    hh_auth = accounts_sub.add_parser("hh-oauth-authorize", help="локальная ссылка авторизации HH")
    hh_auth.add_argument("--account", default=None)
    hh_auth.add_argument("--json", action="store_true")

    hh_exchange = accounts_sub.add_parser(
        "hh-oauth-exchange",
        help="обменять redirect URL или code на токен (state проверяется обязательно)",
    )
    hh_exchange.add_argument("--account", default=None)
    hh_exchange.add_argument(
        "--state", default=None,
        help="state из redirect URL; не нужен, если вставлен полный redirect URL",
    )
    hh_exchange.add_argument("--json", action="store_true")

    hh_status = accounts_sub.add_parser("hh-oauth-status", help="статус токена HH")
    hh_status.add_argument("--account", default=None)
    hh_status.add_argument("--online", action="store_true")
    hh_status.add_argument("--json", action="store_true")

    hh_neg = accounts_sub.add_parser("hh-oauth-negotiations", help="прочитать отклики HH")
    hh_neg.add_argument("--account", default=None)
    hh_neg.add_argument("--limit", type=int, default=20)
    hh_neg.add_argument("--json", action="store_true")

    hh_msgs = accounts_sub.add_parser(
        "hh-oauth-messages", help="read-only: сообщения конкретного отклика HH",
    )
    hh_msgs.add_argument("--account", default=None)
    hh_msgs.add_argument("--negotiation-id", required=True)
    hh_msgs.add_argument("--limit", type=int, default=50)
    hh_msgs.add_argument("--json", action="store_true")

    hh_apply = accounts_sub.add_parser("hh-oauth-apply-plan", help="spec отклика HH без отправки")
    hh_apply.add_argument("--account", default=None)
    hh_apply.add_argument("--vacancy-id", required=True)
    hh_apply.add_argument("--resume-id", required=True)
    hh_apply.add_argument("--message", default="")
    hh_apply.add_argument("--json", action="store_true")

    workflow = sub.add_parser("workflow", help="сохранённые письма и ручные формы")
    workflow_sub = workflow.add_subparsers(dest="workflow_action", required=True)
    workflow_list = workflow_sub.add_parser("list", help="показать постоянные задачи")
    workflow_list.add_argument("--track")
    workflow_list.add_argument("--type", choices=("letter", "form"))
    workflow_list.add_argument("--json", action="store_true")
    workflow_show = workflow_sub.add_parser("show", help="открыть сохранённую задачу")
    workflow_show.add_argument("--track")
    workflow_show.add_argument("--id", required=True)
    letter_preview = workflow_sub.add_parser("letter-preview", help="создать постоянный черновик письма")
    letter_preview.add_argument("--track", required=True)
    letter_preview.add_argument("--id", required=True)
    workflow_edit = workflow_sub.add_parser("edit", help="заменить текст черновика")
    workflow_edit.add_argument("--track", required=True)
    workflow_edit.add_argument("--id", required=True, dest="task_id")
    workflow_edit.add_argument("--text", required=True)
    workflow_approve = workflow_sub.add_parser("approve", help="одобрить точный сохранённый текст")
    workflow_approve.add_argument("--track", required=True)
    workflow_approve.add_argument("--id", required=True, dest="task_id")
    workflow_sent = workflow_sub.add_parser("sent-confirmed", help="отметить ручную отправку письма")
    workflow_sent.add_argument("--track", required=True)
    workflow_sent.add_argument("--id", required=True, dest="task_id")

    inbox = sub.add_parser("inbox", help="только вручную запущенное чтение HH-переписок")
    inbox_sub = inbox.add_subparsers(dest="inbox_action", required=True)
    inbox_sync = inbox_sub.add_parser("sync", help="прочитать ограниченную порцию чатов HH")
    inbox_sync.add_argument("--max-chats", type=int, default=20)
    inbox_sync.add_argument("--messages-per-chat", type=int, default=20)
    inbox_list = inbox_sub.add_parser("list", help="показать сохранённые сообщения")
    inbox_list.add_argument("--thread")
    inbox_list.add_argument("--limit", type=int, default=100)
    inbox_list.add_argument("--json", action="store_true")

    for name, help_text in (("plan", "офлайн-отбор кандидатов"), ("apply", "подготовить или отправить отклики")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--input", required=True, type=Path)
        cmd.add_argument("--limit", type=int, help="максимум выбранных вакансий")
        cmd.add_argument("--min-score", type=int)
        cmd.add_argument("--rescore", action="store_true",
                         help="пересчитать старые snapshots вместо сохранения исторического score")
        cmd.add_argument("--skip-security", action="store_true")
        if name == "apply":
            cmd.add_argument("--dry-run", action="store_true")
            cmd.add_argument("--run", action="store_true")
            cmd.add_argument(
                "--target-success", type=int,
                help="остановиться после указанного числа подтверждённых откликов; --limit задаёт потолок кандидатов",
            )
            mode = cmd.add_mutually_exclusive_group()
            mode.add_argument("--visible", dest="visible", action="store_true",
                              help="открыть реальное окно Chrome (ручная CAPTCHA)")
            mode.add_argument("--headless", dest="headless", action="store_true",
                              help="без окна; CAPTCHA нельзя пройти вручную")
        else:
            cmd.add_argument("--output", type=Path, help="путь к приватному JSON-плану")
        cmd.add_argument("--preset", choices=PRESET_CHOICES)

    inspect_cmd = sub.add_parser("inspect", help="читать страницы вакансий без действий")
    inspect_cmd.add_argument("--input", type=Path)
    inspect_cmd.add_argument("--limit", type=int, default=3)
    inspect_cmd.add_argument("--page-timeout", type=float, default=10.0,
                             help="read-only таймаут одной страницы в секундах")
    inspect_cmd.add_argument("--selected", action="store_true", help="проверить только верхние подтверждённые кандидаты")
    inspect_cmd.add_argument("--resumes", action="store_true", help="прочитать только доступные названия резюме HH")
    inspect_cmd.add_argument("--preset", choices=PRESET_CHOICES)
    inspect_cmd.add_argument("--min-score", type=int)

    llm = sub.add_parser("llm", help="LLM-инструменты")
    llm_sub = llm.add_subparsers(dest="llm_action", required=True)
    preview = llm_sub.add_parser("preview")
    preview.add_argument("--input", required=True, type=Path)
    preview.add_argument("--id", required=True)
    letter = sub.add_parser("letter", help="предпросмотр шаблонного или LLM-письма")
    letter_sub = letter.add_subparsers(dest="letter_action", required=True)
    letter_preview = letter_sub.add_parser("preview")
    letter_preview.add_argument("--input", required=True, type=Path)
    letter_preview.add_argument("--id", required=True)
    rerank = llm_sub.add_parser("rerank", help="явно запустить ограниченный необязательный LLM-rerank")
    rerank.add_argument("--input", required=True, type=Path)
    rerank.add_argument("--model", required=True)
    rerank.add_argument("--limit", type=int, default=20)
    rerank.add_argument("--enable", action="store_true", help="подтвердить внешний LLM-запрос")

    history = sub.add_parser("history", help="инструменты совместимости со старой историей")
    history_sub = history.add_subparsers(dest="history_action", required=True)
    imp = history_sub.add_parser("import")
    imp.add_argument("--source", required=True, type=Path)
    history_sub.add_parser("reconcile").add_argument("--input", required=True, type=Path)
    sync_cmd = sub.add_parser("sync", help="прочитать статусы откликов без сообщений")
    sync_cmd.add_argument("--track", help="использовать профиль и SQLite-контекст трека")
    sync_cmd.add_argument("--pages", type=int,
                          help="максимум страниц откликов; без значения читаются страницы до пустого ответа HH")
    sync_cmd.add_argument("--output", type=Path, help="приватный JSON-отчёт синхронизации")
    analytics_cmd = sub.add_parser("analytics", help="показать локальную статистику откликов")
    analytics_cmd.add_argument("--output", type=Path, help="приватный аналитический отчёт")
    benchmark = sub.add_parser("benchmark", help="запустить офлайн benchmark качества сканера")
    benchmark.add_argument("--suite", default="tech-roles")
    benchmark.add_argument("--control-only", action="store_true")
    review = sub.add_parser("review", help="создать локальный HTML-обзор вакансий")
    review.add_argument("--input", required=True, type=Path)
    review.add_argument("--top", type=int, default=20)
    review.add_argument("--output", type=Path)
    review.add_argument("--preset", choices=PRESET_CHOICES)

    screen = sub.add_parser("screen", help="LLM-скрининг отфильтрованных вакансий (opt-in)")
    screen.add_argument("--input", required=True, type=Path)
    screen.add_argument("--preset", choices=PRESET_CHOICES)
    screen.add_argument("--limit", type=int, default=200)
    screen.add_argument("--min-score", type=int, default=None)
    screen.add_argument("--track", choices=["ai", "infra", "general"], default="general")
    screen.add_argument("--accept", choices=["fit", "fit+maybe"], default="fit+maybe",
                        help="какие вердикты записывать в выходной snapshot")
    screen.add_argument("--model", default=None)
    screen.add_argument("--base-url", default=None,
                        help="OpenAI-compatible endpoint chat-completions")
    screen.add_argument("--concurrency", type=int, default=None)
    screen.add_argument("--criteria", default=None, help="переопределить критерии скрининга в prompt")
    screen.add_argument("--constraints", default=None, help="переопределить ограничения кандидата")
    screen.add_argument("--salary-expectation", default=None, help="переопределить зарплатное ожидание")
    screen.add_argument("--output", type=Path, help="приватный JSON-отчёт вердиктов")
    screen.add_argument("--emit-snapshot", type=Path,
                        help="записать snapshot принятых вакансий для `apply`")
    screen.add_argument("--retry-errors", action="store_true",
                        help="повторить только ERROR-строки и объединить результат с существующим отчётом")
    config_cmd = sub.add_parser("config", help="показать эффективную конфигурацию")
    config_sub = config_cmd.add_subparsers(dest="config_action", required=True)
    config_show = config_sub.add_parser("show", help="показать эффективные настройки поиска")
    config_show.add_argument("--preset", choices=PRESET_CHOICES)
    templates = sub.add_parser("templates", help="показать или создать публичные поисковые шаблоны")
    templates_sub = templates.add_subparsers(dest="templates_action", required=True)
    templates_sub.add_parser("list", help="показать доступные шаблоны")
    template_init = templates_sub.add_parser("init", help="создать новый шаблон без перезаписи")
    template_init.add_argument("--name", required=True, choices=list_templates())
    template_init.add_argument("--output", required=True, type=Path)

    admin = sub.add_parser("admin", help="запустить локальную веб-панель")
    admin.add_argument("--host", default="127.0.0.1")
    admin.add_argument("--port", type=int, default=8765)
    admin.add_argument("--open", action="store_true", help="открыть веб-панель в браузере")
    return parser


def _config(args: argparse.Namespace) -> AppConfig:
    config = AppConfig.discover(data_dir=args.data_dir, profile=args.profile, search=args.search)
    ensure_data_dirs(config)
    # По умолчанию держим браузеры Playwright в private/ и используем уже установленную локальную копию.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.root / "private" / "browsers"))
    return config


def _profile(config: AppConfig, search: dict | None = None) -> dict:
    profile = config.load_profile()
    if search:
        profile = {**profile, "search": search}
        for key in ("role_terms", "required_role_terms", "title_role_terms", "exclude_titles", "salary", "experience",
                    "only_remote", "work_formats"):
            if key in search:
                profile[key] = search[key]
    return profile


def _track_config(config: AppConfig, track: str, *, profile_override: bool = False,
                  search_override: bool = False) -> tuple[AppConfig, dict[str, Any]]:
    """Возвращает конфигурацию выбранного трека с путями, ограниченными tracks.py."""
    from .tracks import load_tracks

    tracks = load_tracks(config.root)
    if track not in tracks:
        raise ConfigError(f"unknown track: {track}")
    item = tracks[track]
    profile_path = config.profile_path if profile_override else config.root / item["profile"]
    search_path = config.search_path if search_override else config.root / item["search"]
    return replace(config, profile_path=profile_path.resolve(), search_path=search_path.resolve()), item


def _campaign_view(config: AppConfig, track: str | None, *, profile_override: bool,
                   search_override: bool) -> tuple[Store, str, str | None]:
    if track:
        selected, _ = _track_config(config, track, profile_override=profile_override,
                                    search_override=search_override)
        profile = _profile(selected, effective_search(selected.load_search()))
        account = _account(profile)
        return Store(selected.db_path), account, track
    profile = _profile(config)
    return Store(config.db_path), _account(profile), None


def _default_track(config: AppConfig) -> str:
    from .tracks import load_tracks
    tracks = load_tracks(config.root)
    return "ai_core" if "ai_core" in tracks else (next(iter(tracks), "") if tracks else "")


def _account(profile: dict) -> str:
    """Использует явный приватный ключ аккаунта с сохранением обратной совместимости."""
    return str(profile.get("account") or "default").strip() or "default"


def _operator_excluded_ids(data_dir: Path) -> set[str]:
    """Возвращает ID, вручную обработанные в UI и не допускаемые до apply."""
    ids: set[str] = set()
    for filename in ("viewed.json", "bad.json", "manual-applied.json"):
        try:
            data = json.loads((data_dir / filename).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, list):
            ids.update(str(value) for value in data if value is not None and str(value).strip())
    return ids


def _default_limit(profile: dict) -> int:
    return int((profile.get("limits") or {}).get("per_run", 100))


def _write_private_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def _default_artifact(config: AppConfig, category: str, suffix: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return config.data_dir / category / f"{category}-{stamp}-{uuid.uuid4().hex[:8]}.{suffix}"


def _print_run_summary(store: Store, run_id: str) -> None:
    summary = store.run_summary(run_id)
    if summary is None:
        return
    run = summary["run"]
    counts = summary["counts"]
    print(f"run_id: {run_id}; run_status: {run['status']}")
    for status in ("success", "already_applied", "needs_manual", "unknown", "failed_before_submit",
                   "skipped", "prepared"):
        print(f"{status}: {counts.get(status, 0)}")
    if run["stop_reason"]:
        print(f"stop_reason: {run['stop_reason']}")


def _print_candidates(items: list[dict], profile: dict, limit: int, min_score: int,
                      skip_security: bool, blocked_ids: set[str] | None = None,
                      rescore: bool = False) -> list[dict]:
    candidates = filter_candidates(items, profile, limit, min_score, skip_security, blocked_ids, rescore)
    for index, candidate in enumerate(candidates, 1):
        print(f"{index:>2}. [{candidate.score:>3}] {candidate.name} — {candidate.company}")
        print(f"    id={candidate.id} resume={candidate.resume or '(профиль не настроен)'}")
        if candidate.reasons:
            print(f"    reasons={', '.join(candidate.reasons)}")
    return [candidate.to_dict() for candidate in candidates]


def _accounts_command(config: AppConfig, args: argparse.Namespace) -> int:
    """Local account management for Telegram MTProto and HH OAuth."""
    from .accounts import (
        DEFAULT_HH_ACCOUNT,
        DEFAULT_TELEGRAM_ACCOUNT,
        telegram_queue_plan,
        unified_accounts,
    )
    from .hh_oauth import (
        HhOAuthAccount,
        HhOAuthError,
        hh_oauth_status,
    )
    from .hh_oauth import (
        save_credentials as save_hh_credentials,
    )
    from .storage import Store
    from .telegram_client import (
        TelegramAccount,
        TelegramNotConfigured,
        TelegramUnavailable,
        telegram_status,
    )
    from .telegram_client import (
        save_credentials as save_tg_credentials,
    )

    root = config.root
    store = Store(config.db_path)
    telegram_account = str(getattr(args, "account", None) or DEFAULT_TELEGRAM_ACCOUNT).strip() or DEFAULT_TELEGRAM_ACCOUNT
    hh_account = str(getattr(args, "account", None) or DEFAULT_HH_ACCOUNT).strip() or DEFAULT_HH_ACCOUNT
    track = str(getattr(args, "track", None) or _default_track(config)).strip()

    def emit(payload: object) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    try:
        if args.accounts_action == "list":
            payload = unified_accounts(root, telegram_account=telegram_account, hh_account=hh_account)
            payload["last_checks"] = store.external_accounts()
            if args.json:
                emit(payload)
            else:
                for provider in ("telegram", "hh_oauth"):
                    item = payload[provider]
                    print(f"{provider}: {item.get('status')} · account={item.get('account')} · "
                          f"apply={item.get('apply_status', '')}")
            return 0

        if args.accounts_action == "check":
            payload = unified_accounts(root, telegram_account=telegram_account, hh_account=hh_account)
            if args.online:
                payload["telegram"] = TelegramAccount(root, telegram_account).status()
                payload["hh_oauth"] = HhOAuthAccount(root, hh_account).status(check_remote=True)
                for provider, item in (("telegram", payload["telegram"]), ("hh_oauth", payload["hh_oauth"])):
                    store.save_external_account(
                        provider, item.get("account", ""), str(item.get("status", "")),
                        str(item.get("detail", "")), {"source": "accounts check"},
                    )
            if args.json:
                emit(payload)
            else:
                for provider in ("telegram", "hh_oauth"):
                    item = payload[provider]
                    print(f"{provider}: {item.get('status')} · account={item.get('account')} · "
                          f"detail={item.get('detail', '')}")
            return 0

        if args.accounts_action == "telegram-setup":
            api_id = args.api_id
            if api_id is None:
                api_id = int(input("Telegram API_ID (my.telegram.org): ").strip())
            api_hash = getpass.getpass("Telegram API_HASH (hidden): ").strip()
            path = save_tg_credentials(root, api_id, api_hash)
            payload = {
                "status": "configured",
                "path": str(path),
                "api_id": api_id,
                "api_hash": "***",
                "next": "applypilot accounts telegram-login --phone +<your number>",
            }
            if args.json:
                emit(payload)
            else:
                print(f"Telegram credentials saved to {path} (0600). Next: "
                      "applypilot accounts telegram-login --phone +<your number>")
            return 0

        if args.accounts_action == "telegram-login":
            phone = args.phone or input("Telegram phone (+...): ").strip()
            result = TelegramAccount(root, telegram_account).login(
                phone,
                code_callback=lambda: getpass.getpass("Telegram code (hidden): ").strip(),
                password_callback=lambda: getpass.getpass("Telegram 2FA password (hidden): ").strip(),
            )
            store.save_external_account(
                "telegram", telegram_account, str(result.get("status", "")),
                str(result.get("detail", "")), {"source": "telegram-login"},
            )
            if args.json:
                emit(result)
            else:
                print(f"telegram: {result.get('status')} · account={telegram_account} · "
                      f"session={result.get('session')}")
            return 0 if result.get("status") == "confirmed" else 2

        if args.accounts_action == "telegram-status":
            result = (
                TelegramAccount(root, telegram_account).status()
                if args.online else telegram_status(root, telegram_account)
            )
            store.save_external_account(
                "telegram", telegram_account, str(result.get("status", "")),
                str(result.get("detail", "")), {"source": "telegram-status"},
            )
            if args.json:
                emit(result)
            else:
                print(f"telegram: {result.get('status')} · account={telegram_account} · "
                      f"session_present={result.get('session_present')}")
            return 0

        if args.accounts_action == "telegram-history":
            messages = TelegramAccount(root, telegram_account).history(args.limit)
            public = [
                {
                    "message_id": item.get("message_id"),
                    "sent_at": item.get("sent_at"),
                    "text": item.get("text"),
                    "buttons": [
                        {key: value for key, value in button.items() if key != "callback_sha256"}
                        for button in item.get("buttons", [])
                    ],
                    "vacancy_refs": item.get("vacancy_refs", []),
                    "application_likely": item.get("application_likely"),
                }
                for item in messages
            ]
            if args.json:
                emit(public)
            else:
                for item in public:
                    print(f"{item['message_id']} · {item['sent_at']} · {item['text'][:120]!r} · "
                          f"buttons={len(item['buttons'])}")
            return 0

        if args.accounts_action == "telegram-sync":
            result = TelegramAccount(root, telegram_account).sync(store, args.limit)
            result["plan"] = telegram_queue_plan(store, telegram_account, track, args.limit)
            store.save_external_account(
                "telegram", telegram_account, "ok", "history synced",
                {"messages": result.get("messages"), "saved": result.get("saved")},
            )
            if args.json:
                emit(result)
            else:
                print(f"telegram: synced {result.get('messages')} message(s), saved {result.get('saved')}; "
                      f"planned {result['plan'].get('planned_count')} dry-run item(s)")
            return 0

        if args.accounts_action == "telegram-plan":
            result = telegram_queue_plan(store, telegram_account, track, args.limit)
            if args.json:
                emit(result)
            else:
                print(f"telegram plan: {result.get('planned_count')} item(s), "
                      f"{result.get('unmatched_count')} unmatched · mode=dry_run")
            return 0

        if args.accounts_action == "telegram-import":
            from .multisource import resolve_source_account
            from .telegram_import import import_telegram_vacancies

            ledger_account = resolve_source_account(config, config.load_profile(), args.source)
            result = import_telegram_vacancies(
                store, account=telegram_account, source=args.source, track=track,
                limit=args.limit, create_missing=bool(args.create_missing),
                ledger_account=ledger_account,
            )
            if args.json:
                emit(result)
            else:
                print(
                    f"telegram import: linked={result['linked_count']} "
                    f"created={result['created_count']} missing={result['missing_count']}"
                )
            return 0

        if args.accounts_action == "hh-oauth-setup":
            client_id = args.client_id or input("HH OAuth client_id: ").strip()
            client_secret = getpass.getpass("HH OAuth client_secret (hidden): ").strip()
            redirect_uri = args.redirect_uri or input(
                "HH OAuth redirect_uri [http://localhost:8765/callback]: "
            ).strip() or "http://localhost:8765/callback"
            path = save_hh_credentials(root, client_id, client_secret, redirect_uri)
            payload = {
                "status": "configured", "path": str(path), "client_id": client_id,
                "client_secret": "***", "redirect_uri": redirect_uri,
                "next": "applypilot accounts hh-oauth-authorize",
            }
            if args.json:
                emit(payload)
            else:
                print(f"HH OAuth credentials saved to {path} (0600). Next: "
                      "applypilot accounts hh-oauth-authorize")
            return 0

        if args.accounts_action == "hh-oauth-authorize":
            result = HhOAuthAccount(root, hh_account).authorize_url()
            store.save_external_account(
                "hh_oauth", hh_account, "authorization_required", "local authorize URL issued",
                {"redirect_uri": result.get("redirect_uri")},
            )
            if args.json:
                emit(result)
            else:
                print(f"Open locally:\n{result.get('url')}\nstate={result.get('state')}")
            return 0

        if args.accounts_action == "hh-oauth-exchange":
            code = getpass.getpass(
                "HH redirect URL or authorization code (hidden): "
            ).strip()
            result = HhOAuthAccount(root, hh_account).exchange_code(code, args.state)
            store.save_external_account(
                "hh_oauth", hh_account, "authorized", "token saved",
                {"expires_at": result.get("expires_at")},
            )
            if args.json:
                emit(result)
            else:
                print(f"hh_oauth: authorized · account={hh_account} · expires_at={result.get('expires_at')}")
            return 0

        if args.accounts_action == "hh-oauth-status":
            result = (
                HhOAuthAccount(root, hh_account).status(check_remote=True)
                if args.online else hh_oauth_status(root, hh_account)
            )
            store.save_external_account(
                "hh_oauth", hh_account, str(result.get("status", "")),
                str(result.get("detail", "")), {"source": "hh-oauth-status"},
            )
            if args.json:
                emit(result)
            else:
                print(f"hh_oauth: {result.get('status')} · account={hh_account} · "
                      f"token_present={result.get('token_present', False)}")
            return 0

        if args.accounts_action == "hh-oauth-negotiations":
            rows = HhOAuthAccount(root, hh_account).negotiations(args.limit)
            if args.json:
                emit(rows)
            else:
                print(f"hh_oauth negotiations: {len(rows)}")
            return 0

        if args.accounts_action == "hh-oauth-messages":
            rows = HhOAuthAccount(root, hh_account).messages(
                args.negotiation_id, args.limit,
            )
            if args.json:
                emit(rows)
            else:
                print(f"hh_oauth messages: {len(rows)} · negotiation={args.negotiation_id}")
                for row in rows:
                    print(f"{row.get('created_at', '')} · {row.get('author', '')} · "
                          f"{row.get('text', '')}")
            return 0

        if args.accounts_action == "hh-oauth-apply-plan":
            spec = HhOAuthAccount.apply_spec(args.vacancy_id, args.resume_id, args.message)
            if args.json:
                emit(spec)
            else:
                print(json.dumps(spec, ensure_ascii=False, indent=2))
            return 0
    except (ConfigError, ValueError, OSError, HhOAuthError, TelegramNotConfigured, TelegramUnavailable) as exc:
        print(f"accounts: error ({str(exc)[:240]})", file=sys.stderr)
        return 2
    print(f"accounts: unknown action {args.accounts_action}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config(args)

    if args.command == "doctor":
        print(f"root: {config.root}")
        print(f"data: {config.data_dir}")
        print(f"profile: {'present' if config.profile_path.exists() else 'missing'}")
        print("playwright: lazy (install optional browser extra)")
        if args.online:
            try:
                import requests
                response = requests.get("https://hh.ru", timeout=10, headers={"User-Agent": "ApplyPilot/0.1"})
                print(f"network: HTTP {response.status_code}")
            except requests.RequestException as exc:
                print(f"network: error ({exc})")
        return 0

    if args.command == "admin":
        # Сохраняем CLI/headless-путь независимым от необязательной веб-панели.
        from .admin import serve as admin_serve

        try:
            admin_serve(config, host=args.host, port=args.port, open_browser=args.open)
        except (ConfigError, ValueError) as exc:
            print(f"admin: {exc}", file=sys.stderr)
            return 2
        return 0

    session_path = config.data_dir / "hh_session.json"
    if args.command == "login":
        login(session_path, data_dir=config.data_dir, profile=config.load_profile())
        print(f"saved: {session_path}")
        return 0
    if args.command == "session":
        from .session import check_session
        result = check_session(session_path, data_dir=config.data_dir,
                               profile=config.load_profile())
        print(f"session: {result.status} ({result.detail})")
        return 0 if result.status in {"confirmed", "valid-format"} else 1
    if args.command == "sources":
        from .multisource import queue_plan, scan_sources, source_status
        from .providers import default_registry

        registry = default_registry()
        if args.sources_action == "list":
            rows = registry.catalogue()
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    caps = row["capabilities"]
                    enabled = "scan" if caps["discover"] else "read-only"
                    print(f"{row['source']} · {row['label']} · {enabled} · "
                          f"login={'yes' if caps['login'] else 'no'} · "
                          f"apply={caps['apply_status']}")
            return 0
        if args.sources_action == "status":
            selected_config, _track_info = _track_config(
                config, args.track, profile_override=bool(args.profile),
                search_override=bool(args.search),
            )
            search = effective_search(selected_config.load_search())
            result = source_status(selected_config, args.track, None, registry=registry)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"sources: track={args.track}; account={result['account']}")
                for row in result["sources"]:
                    print(f"  {row['source']}: account={row['account']} queue={row['queue']} "
                          f"reviewable={row.get('reviewable', 0)} approved={row['approved']} "
                          f"apply={row['capabilities']['apply_status']}")
            return 0
        if args.sources_action in {"login", "check"}:
            from .multisource import resolve_source_account
            from .provider_sessions import ProviderSessionManager
            selected_config, _track_info = _track_config(
                config, args.track if getattr(args, "track", None) else _default_track(config),
                profile_override=bool(args.profile), search_override=bool(args.search),
            )
            profile = _profile(selected_config, effective_search(selected_config.load_search()))
            account = args.account or resolve_source_account(selected_config, profile, args.source)
            manager = ProviderSessionManager(config.data_dir / "providers")
            if args.sources_action == "login":
                result = manager.login(
                    args.source, account,
                    wait_seconds=args.wait_seconds,
                    interactive=not args.no_interactive,
                )
            else:
                result = manager.check(args.source, account)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"{args.source}/{account}: {result.get('status')} — {result.get('detail','')}")
            return 0 if result.get("status") in {"confirmed", "valid-format", "saved"} else 1
        if args.sources_action == "form-audit":
            if args.source != "career.habr.com":
                print("form-audit: this source has no verified read-only form inspector", file=sys.stderr)
                return 2
            if not 1 <= args.limit <= 100 or not 0 <= args.delay_seconds <= 20:
                print("form-audit: limit must be 1..100 and delay 0..20 seconds", file=sys.stderr)
                return 2
            from .habr_apply import inspect_habr_page
            from .multisource import resolve_source_account
            from .provider_sessions import ProviderSessionManager, _exclusive_profile_lock

            selected_config, _ = _track_config(
                config, args.track, profile_override=bool(args.profile),
                search_override=bool(args.search),
            )
            profile = _profile(
                selected_config, effective_search(selected_config.load_search()),
            )
            account = resolve_source_account(
                selected_config, profile, args.source,
            )
            manager = ProviderSessionManager(selected_config.data_dir / "providers")
            auth = manager.check(args.source, account)
            if auth.get("status") != "confirmed":
                print(
                    "form-audit: Habr session is not confirmed; run sources login/check",
                    file=sys.stderr,
                )
                return 2
            rows = Store(selected_config.db_path).list_campaign_vacancies(
                account, args.track, args.view, args.limit, source=args.source,
            )
            from playwright.sync_api import sync_playwright

            results: list[dict[str, Any]] = []
            profile_dir = manager.profile_dir(args.source, account)
            with _exclusive_profile_lock(profile_dir.parent), sync_playwright() as playwright:
                context = manager._context_factory(playwright, profile_dir, True)
                try:
                    # Page views only: even unexpected provider scripts must
                    # not mutate a profile or submit a candidate application.
                    context.route(
                        "**/*",
                        lambda route: (
                            route.continue_()
                            if route.request.method.upper() in {"GET", "HEAD", "OPTIONS"}
                            else route.abort()
                        ),
                    )
                    for index, row in enumerate(rows):
                        if index and args.delay_seconds:
                            time.sleep(args.delay_seconds)
                        page = context.new_page()
                        try:
                            results.append(inspect_habr_page(page, row))
                        finally:
                            page.close()
                finally:
                    context.close()
            counts: dict[str, int] = {}
            for item in results:
                state = str(item["status"])
                counts[state] = counts.get(state, 0) + 1
            summary = {
                "mode": "read_only", "source": args.source, "track": args.track,
                "account": account, "view": args.view, "inspected": len(results),
                "counts": counts, "results": results, "submitted": False,
            }
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                print(f"Habr read-only form audit: {len(results)} vacancies; {counts}")
                for item in results:
                    print(f"  {item['id']} score={item['score']} status={item['status']}")
            return 0
        if args.sources_action == "scan":
            selected_config, _track_info = _track_config(
                config, args.track, profile_override=bool(args.profile),
                search_override=bool(args.search),
            )
            search = effective_search(selected_config.load_search())
            profile = _profile(selected_config, search)
            sources = args.source or None
            result = scan_sources(
                selected_config, args.track, profile, search, sources=sources,
                pages=args.pages, request_budget=args.request_budget,
                details_limit=args.details_limit, limit=args.limit,
                fixture_dir=args.fixture_dir, telegram_hints=args.telegram_hints,
                registry=registry,
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"multi-source scan: track={args.track}; sources={len(result['sources'])}")
                for row in result["sources"]:
                    print(f"  {row['source']}: {row['status']} · items={row.get('items', 0)} · "
                          f"campaign={row.get('campaign_id','-')}")
                    for error in row.get("errors", [])[:4]:
                        print(f"    note: {error[:220]}")
            return 0 if not result["failed_sources"] else 2
        if args.sources_action == "approve":
            selected_config, _track_info = _track_config(
                config, args.track, profile_override=bool(args.profile),
                search_override=bool(args.search),
            )
            search = effective_search(selected_config.load_search())
            from .multisource import resolve_source_account
            account = resolve_source_account(
                selected_config, _profile(selected_config, search), args.source,
            )
            changed = Store(selected_config.db_path).set_review_state_batch(
                account, args.source, args.id, args.state, reason=args.reason, track=args.track,
            )
            print(f"sources: {changed} -> {args.state.upper()} ({args.source}, track={args.track})")
            return 0
        if args.sources_action == "plan":
            selected_config, _track_info = _track_config(
                config, args.track, profile_override=bool(args.profile),
                search_override=bool(args.search),
            )
            search = effective_search(selected_config.load_search())
            from .multisource import resolve_source_account
            account = resolve_source_account(
                selected_config, _profile(selected_config, search), args.source,
            )
            plan = queue_plan(Store(selected_config.db_path), account, args.track,
                              args.source, args.limit, registry=registry)
            if args.json:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
            else:
                print(f"source dry-run: {args.source}; planned={plan['planned_count']} "
                      f"blocked={plan['blocked_count']} · apply={plan['apply_status']}")
                for row in plan["planned"]:
                    print(f"  PLAN {row['id']} · {row['name']} — {row['company']}")
                for row in plan["blocked"]:
                    print(f"  BLOCKED {row['id']} · {row['reason']}")
            return 0
        if args.sources_action in {"dispatch-plan", "dispatch-apply"}:
            selected_config, _track_info = _track_config(
                config, args.track, profile_override=bool(args.profile),
                search_override=bool(args.search),
            )
            from .dispatch import dispatch_apply, dispatch_plan
            from .multisource import resolve_source_account

            profile = _profile(selected_config, effective_search(selected_config.load_search()))
            selected = args.source or [row["source"] for row in registry.catalogue()]
            accounts = {
                source: resolve_source_account(selected_config, profile, source)
                for source in selected
            }
            store = Store(selected_config.db_path)
            if args.sources_action == "dispatch-plan":
                result = dispatch_plan(
                    store, "default", args.track, selected, args.limit,
                    accounts=accounts, registry=registry,
                )
            else:
                if args.enable_habr and not args.allow_live:
                    print("dispatch: --enable-habr requires --allow-live", file=sys.stderr)
                    return 2
                if not 0 <= args.habr_min_interval <= 30:
                    print("dispatch: --habr-min-interval must be 0..30", file=sys.stderr)
                    return 2
                drivers: dict[str, Any] = {}
                with ExitStack() as cleanup:
                    if args.allow_live and args.enable_habr and "career.habr.com" in selected:
                        from .habr_apply import HabrPageDriver
                        from .provider_sessions import (
                            ProviderSessionManager,
                            _exclusive_profile_lock,
                        )

                        manager = ProviderSessionManager(selected_config.data_dir / "providers")
                        habr_account = accounts["career.habr.com"]
                        auth = manager.check("career.habr.com", habr_account)
                        if auth.get("status") == "confirmed":
                            from playwright.sync_api import sync_playwright

                            profile_dir = manager.profile_dir("career.habr.com", habr_account)
                            cleanup.enter_context(_exclusive_profile_lock(profile_dir.parent))
                            playwright = cleanup.enter_context(sync_playwright())
                            context = manager._context_factory(playwright, profile_dir, not args.visible)
                            cleanup.callback(context.close)
                            drivers["career.habr.com"] = HabrPageDriver(
                                context.new_page, min_interval_seconds=args.habr_min_interval,
                            )
                        else:
                            print(
                                f"Habr driver unavailable: login={auth.get('status', 'unknown')}; "
                                "run sources login/check first",
                                file=sys.stderr,
                            )
                    result = dispatch_apply(
                        store, "default", args.track, selected, args.limit,
                        allow_live=bool(args.allow_live), drivers=drivers,
                        accounts=accounts, registry=registry,
                    )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(
                    f"dispatch {result['mode']}: sources={len(result['sources'])} "
                    f"planned={result['planned_count']} blocked={result['blocked_count']} "
                    f"executed={result.get('executed', 0)}"
                )
                for row in result["planned"]:
                    print(f"  PLAN {row['source']}:{row['id']} · {row['name']} — {row['company']}")
                for row in result["blocked"]:
                    print(f"  BLOCKED {row['source']}:{row['id']} · {row['reason']}")
                for row in result.get("results", []):
                    print(f"  RESULT {row['source']}:{row['id']} · {row['status']} · {row.get('reason', '')}")
            return 0
        if args.sources_action == "approve-batch":
            selected_config, _track_info = _track_config(
                config, args.track, profile_override=bool(args.profile),
                search_override=bool(args.search),
            )
            from .dispatch import approve_batch
            from .multisource import resolve_source_account

            profile = _profile(selected_config, effective_search(selected_config.load_search()))
            account = resolve_source_account(selected_config, profile, args.source)
            store = Store(selected_config.db_path)
            ids: list[str] = []
            for chunk in (args.id or []):
                ids.extend(value.strip() for value in chunk.split(",") if value.strip())
            if not ids and args.from_view:
                rows = store.list_campaign_vacancies(
                    account, args.track, args.from_view, args.limit, source=args.source,
                )
                ids = [str(row["id"]) for row in rows]
            result = approve_batch(
                store, account, args.track, args.source, ids,
                state=args.state, reason=args.reason,
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(
                    f"approve-batch {result['source']}: requested={result['requested']} "
                    f"updated={result['updated']} state={result['state']}"
                )
            return 0
        if args.sources_action == "accounts":
            selected_config, _track_info = _track_config(
                config, args.track, profile_override=bool(args.profile),
                search_override=bool(args.search),
            )
            from .multisource import resolve_source_account
            profile = _profile(selected_config, effective_search(selected_config.load_search()))
            rows = [
                {"source": row["source"], "label": row["label"],
                 "account": resolve_source_account(selected_config, profile, row["source"]),
                 "capabilities": row["capabilities"]}
                for row in registry.catalogue()
            ]
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(f"{row['source']} -> {row['account']} · apply={row['capabilities']['apply_status']}")
            return 0
        if args.sources_action == "recover":
            store = Store(config.db_path)
            rows = store.recover_stale_source_reservations(
                max_age_seconds=args.max_age_seconds, account=args.account,
            )
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print(f"recovered/quarantined: {len(rows)}")
                for row in rows:
                    print(f"  {row['source']}:{row['vacancy_id']} · {row['status']} · {row['run_id']}")
            return 0
        if args.sources_action == "reconcile":
            selected_config, _track_info = _track_config(
                config, args.track, profile_override=bool(args.profile),
                search_override=bool(args.search),
            )
            from .multisource import resolve_source_account
            profile = _profile(selected_config, effective_search(selected_config.load_search()))
            account = resolve_source_account(selected_config, profile, args.source)
            ok = Store(selected_config.db_path).reconcile_source_application(
                account, args.source, args.id, args.track, args.channel,
                args.evidence, note=args.note,
            )
            print(f"reconcile {args.source}:{args.id}: {'confirmed' if ok else 'rejected'}")
            return 0 if ok else 2
        if args.sources_action == "attempts":
            store = Store(config.db_path)
            if args.source:
                rows = store.source_attempts("default", args.source, args.limit)
            else:
                rows = []
                for source in registry.sources():
                    rows.extend(store.source_attempts("default", source, args.limit))
            rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
            if args.json:
                print(json.dumps(rows[:args.limit], ensure_ascii=False, indent=2))
            else:
                for row in rows[:args.limit]:
                    print(f"{row['updated_at']} {row['source']}:{row['vacancy_id']} "
                          f"{row['status']} {row.get('note','')}")
            return 0
        return 2
    if args.command == "run" and args.run_action == "bundle":
        from .bundles import compile_bundle
        from .campaigns import run_campaign

        if not 1 <= args.limit <= 100 or not 1 <= args.days <= 365:
            print("bundle: --limit must be 1-100 and --days 1-365", file=sys.stderr)
            return 2
        tracks = list(dict.fromkeys(args.track or ["ai", "backend", "ml"]))
        reports: dict[str, dict[str, Any]] = {}
        failures: dict[str, str] = {}
        account = ""
        for track in tracks:
            try:
                selected_config, track_info = _track_config(
                    config, track, profile_override=bool(args.profile),
                    search_override=bool(args.search),
                )
                if track == "ai" and args.wide_ai and not args.search:
                    broad = config.root / "private" / "config" / "search-ai-broad.toml"
                    if broad.is_file():
                        selected_config = replace(selected_config, search_path=broad.resolve())
                search = effective_search(selected_config.load_search())
                profile = _profile(selected_config, search)
                current_account = _account(profile)
                if account and current_account != account:
                    raise ValueError("all tracks must share the same HH account")
                account = current_account
                print(f"bundle: scanning {track}", flush=True)
                reports[track] = run_campaign(
                    selected_config, track, profile, search,
                    days=args.days, pages=args.pages,
                    request_budget=args.request_budget, details_limit=args.details_limit,
                    limit=args.limit, only_remote=args.remote,
                    salary_from=args.salary_from, salary_currency="RUR",
                    sort_mode=args.sort_mode, track_resume=str(track_info.get("resume") or ""),
                )
                print(f"bundle: {track} found={reports[track]['summary']['items']}"
                      f" queries={reports[track]['covered_query_count']}/{reports[track]['query_count']}"
                      f" cached={reports[track]['cached_descriptions']}"
                      f" fetched={reports[track]['detail_fetch_attempts']}"
                      f" status={reports[track]['status']}", flush=True)
            except (Exception, KeyboardInterrupt) as exc:
                if isinstance(exc, KeyboardInterrupt):
                    raise
                failures[track] = f"{type(exc).__name__}: {str(exc)[:180]}"
                print(f"bundle: {track} FAILED ({failures[track]})", file=sys.stderr, flush=True)
        if not reports:
            print("bundle: no tracks completed", file=sys.stderr)
            return 3
        result = compile_bundle(Store(config.db_path), account, reports, limit=args.limit)
        result["failed_tracks"] = failures
        result["created_utc"] = datetime.now(UTC).isoformat()
        output_dir = config.data_dir / "bundles"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"bundle-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"bundle: raw_rows={result['raw_rows_by_track']}"
              f" unique_raw={result['unique_raw_vacancies']}"
              f" new_to_sqlite={result['first_seen_sqlite_unique']}"
              f" new_since_previous={result['new_since_previous_unique']}"
              f" published_last24h={result['published_last_24h_unique']}")
        print(f"bundle: unique_candidates={result['unique_candidates']}"
              f" fit={result['fit_candidates']}"
              f" needs_enrichment={result['needs_enrichment_candidates']}"
              f" duplicates={result['duplicate_postings_suppressed']}"
              f" review={result['review_count']} failures={len(failures)}")
        print(f"bundle_report: {output}")
        for index, item in enumerate(result["items"][:20], 1):
            print(f"{index:>2}. [{item['track']}] {item['id']} score={item['score']}"
                  f" rank={item['review_rank']} {item['name'][:85]} · {item['company'][:32]}")
        print("No applications submitted. Use batch-set and queue apply after selecting good fits.")
        return 2 if failures else 0
    if args.command == "run" and args.run_action == "campaign":
        try:
            selected_config, track_info = _track_config(
                config, args.track, profile_override=bool(args.profile),
                search_override=bool(args.search),
            )
            search = effective_search(selected_config.load_search(), args.preset)
            profile = _profile(selected_config, search)
            from .campaigns import run_campaign

            result = run_campaign(
                selected_config, args.track, profile, search, queries=args.query, areas=args.area,
                days=args.days, pages=args.pages, request_budget=args.request_budget,
                details_limit=args.details_limit, limit=args.limit, only_remote=args.remote,
                salary_from=args.salary_from, salary_currency=args.salary_currency,
                sort_mode=args.sort_mode, track_resume=str(track_info.get("resume") or ""),
                preset=args.preset,
            )
        except (ConfigError, ValueError, OSError) as exc:
            print(f"campaign: error ({str(exc)[:240]})", file=sys.stderr)
            return 2
        except Exception as exc:
            LOGGER.exception("Manual campaign failed")
            print(f"campaign: error ({type(exc).__name__}: {str(exc)[:200]})", file=sys.stderr)
            return 3
        counts = result["summary"]
        print(f"campaign: {result['campaign_id']} · track={result['track']} · status={result['status']}")
        print(f"Найдено: {counts['items']} · впервые: {counts['first_seen']} · "
              f"новые после предыдущего scan: {counts['new_since_previous']} · "
              f"обновлены: {counts['updated']} · уже известны: {counts['known']}")
        print(f"FIT: {result['score_counts']['fit']} · MAYBE/не хватает данных: {result['score_counts']['maybe']} · "
              f"SKIP по критериям: {result['score_counts']['skip']} · "
              f"уже есть в истории откликов/HH: {counts['applied_before']} · ошибки/неполнота: {counts['errors']}")
        print(f"snapshot: {result['snapshot']}")
        if result["segment_errors"]:
            print("scan_notes:")
            for error in result["segment_errors"][:12]:
                print(f"  - {error[:240]}")
        print(f"\nТоп очереди (эвристическая сортировка; это не вероятность оффера): {len(result['top'])}")
        for index, item in enumerate(result["top"], 1):
            problems = ", ".join(item.get("problems") or [])
            reasons = ", ".join(item.get("reasons") or [])
            print(f"{index:>3}. [{item.get('score_decision','?')}/{item.get('filter_status','?')}] "
                  f"{item.get('score',0):>3} · {item.get('id')} · {item.get('name','')}")
            print(f"     {item.get('company','')} · зарплата: {item.get('salary') or 'не опубликована'} · "
                  f"решение: {item.get('review_state','UNREVIEWED')} · "
                  f"отклик: {item.get('application_state','NOT_STARTED')}")
            if reasons:
                print(f"     за: {reasons[:260]}")
            if problems or item.get("filter_reasons"):
                print(f"     проверить: {', '.join([*item.get('filter_reasons', []), *item.get('problems', [])])[:300]}")
            if item.get("url"):
                print(f"     {item['url']}")
        return 2 if result["status"] == "partial" else 0
    if args.command == "queue":
        try:
            if args.queue_action == "list":
                store, account, track = _campaign_view(
                    config, args.track, profile_override=bool(args.profile),
                    search_override=bool(args.search),
                )
                rows = store.list_campaign_vacancies(
                    account, track, args.view, args.limit, source=args.source,
                )
                if args.json:
                    print(json.dumps({"account": account, "track": track, "view": args.view,
                                      "count": len(rows), "items": rows}, ensure_ascii=False, indent=2))
                    return 0
                print(f"queue: {args.view}; track={track or 'all'}; items={len(rows)}")
                for index, item in enumerate(rows, 1):
                    print(f"{index:>3}. {item.get('id')} · {item.get('review_state')} · "
                          f"{item.get('application_state')} · score={item.get('score')} · "
                          f"{item.get('name','')} — {item.get('company','')}")
                    print(f"     обнаружена: {item.get('discovery_status')} · "
                          f"фильтр: {item.get('filter_status')} · причины: "
                          f"{', '.join(item.get('reasons') or [])[:220]}")
                    if item.get("problems") or item.get("filter_reasons"):
                        print(f"     проверить: {', '.join([*item.get('filter_reasons', []), *item.get('problems', [])])[:260]}")
                    if item.get("url"):
                        print(f"     {item['url']}")
                return 0
            if args.queue_action == "history":
                store, account, track = _campaign_view(
                    config, args.track, profile_override=bool(args.profile),
                    search_override=bool(args.search),
                )
                rows = store.campaign_history(account, track, args.limit)
                if args.json:
                    print(json.dumps(rows, ensure_ascii=False, indent=2))
                else:
                    for row in rows:
                        print(f"{row['campaign_id']} · {row['track']} · {row['status']} · "
                              f"{row['started_at']} · items={row['item_count']} · "
                              f"new={row['first_seen_count'] + row['new_count']} · "
                              f"updated={row['updated_count']} · errors={row['error_count']}")
                        print(f"     {row['snapshot_path']}")
                return 0
            if args.queue_action == "set":
                selected_config, _track_info = _track_config(
                    config, args.track, profile_override=bool(args.profile),
                    search_override=bool(args.search),
                )
                search = effective_search(selected_config.load_search())
                account = _account(_profile(selected_config, search))
                Store(selected_config.db_path).set_review_state(
                    account, args.source, args.id, args.state, reason=args.reason, notes=args.notes,
                    track=args.track,
                )
                print(f"queue: {args.id} -> {args.state.upper()} (track={args.track})")
                return 0
            if args.queue_action == "batch-set":
                selected_config, _track_info = _track_config(
                    config, args.track, profile_override=bool(args.profile),
                    search_override=bool(args.search),
                )
                search = effective_search(selected_config.load_search())
                account = _account(_profile(selected_config, search))
                changed = Store(selected_config.db_path).set_review_state_batch(
                    account, args.source, args.id, args.state, reason=args.reason, track=args.track,
                )
                print(f"queue: batch={changed} -> {args.state.upper()} (track={args.track})")
                return 0
            if args.queue_action == "apply":
                if args.limit < 1 or args.limit > 100:
                    print("--limit must be between 1 and 100", file=sys.stderr)
                    return 2
                if args.target_success is not None and (args.target_success < 1 or args.target_success > args.limit):
                    print("--target-success must be positive and no greater than --limit", file=sys.stderr)
                    return 2
                selected_config, track_info = _track_config(
                    config, args.track, profile_override=bool(args.profile),
                    search_override=bool(args.search),
                )
                search = effective_search(selected_config.load_search())
                profile = _profile(selected_config, search)
                account = _account(profile)
                store = Store(selected_config.db_path)
                if args.source != "hh.ru":
                    from .multisource import queue_plan
                    plan = queue_plan(store, account, args.track, args.source, args.limit)
                    print(f"source dry-run: {args.source}; planned={plan['planned_count']} "
                          f"blocked={plan['blocked_count']} · apply={plan['apply_status']}")
                    for row in plan["planned"]:
                        print(f"  PLAN {row['id']} · {row['name']} — {row['company']}")
                    for row in plan["blocked"]:
                        print(f"  BLOCKED {row['id']} · {row['reason']}")
                    if args.run:
                        print("real submit is disabled for this source: needs_live_validation",
                              file=sys.stderr)
                        return 2
                    return 0
                from .campaigns import write_approved_input

                input_path, selected = write_approved_input(selected_config, args.track, account, args.limit)
                if input_path is None or not selected:
                    print("approved queue is empty or every approved vacancy is blocked", file=sys.stderr)
                    return 2
                expected_resume = str(track_info.get("resume") or "").strip()
                if not expected_resume:
                    print("track has no exact HH resume configured; apply is blocked", file=sys.stderr)
                    return 2
                mismatches = [str(item.get("id")) for item in selected
                              if str(item.get("resume") or "").strip() != expected_resume]
                if mismatches:
                    print(f"resume mismatch; expected {expected_resume!r}; blocked IDs: {', '.join(mismatches)}",
                          file=sys.stderr)
                    return 2
                run_id = uuid.uuid4().hex
                if args.dry_run:
                    print(f"approved items: {len(selected)}; sync_status="
                          f"{(store.latest_sync(account) or {}).get('status','missing')} (dry-run)")
                    return _run_dry(store, selected, run_id, account, input_path, args.limit)
                sync = store.latest_sync(account)
                max_age_hours = max(1, int((profile.get("apply") or {}).get("sync_max_age_hours", 24)))
                fresh = False
                if sync and sync.get("status") == "ok":
                    try:
                        fetched_at = datetime.fromisoformat(str(sync["created_at"]))
                        if fetched_at.tzinfo is None:
                            fetched_at = fetched_at.replace(tzinfo=UTC)
                        fresh = datetime.now(UTC) - fetched_at.astimezone(UTC) <= timedelta(hours=max_age_hours)
                    except (KeyError, ValueError):
                        fresh = False
                if not fresh:
                    print(f"sync_stale: run `applypilot sync --track {args.track}` successfully before --run "
                          f"(max age {max_age_hours}h)", file=sys.stderr)
                    return 2
                print(f"explicit apply run: {len(selected)} approved vacancy/vacancies; sync age <= {max_age_hours}h")
                return _run_apply(
                    selected_config, store, selected, run_id, account,
                    input_path, args.limit, args.target_success,
                    headless_override=(True if args.headless else (False if args.visible else None)),
                )
        except (ConfigError, ValueError, OSError) as exc:
            print(f"queue: error ({str(exc)[:240]})", file=sys.stderr)
            return 2
    if args.command == "accounts":
        return _accounts_command(config, args)
    if args.command == "workflow":
        try:
            if args.workflow_action == "list":
                store, account, track = _campaign_view(
                    config, args.track, profile_override=bool(args.profile),
                    search_override=bool(args.search),
                )
                tasks = store.workflow_tasks(account, args.type)
                if track:
                    tasks = [task for task in tasks if task.get("track") == track]
                if args.json:
                    print(json.dumps(tasks, ensure_ascii=False, indent=2))
                else:
                    for task in tasks:
                        print(f"{task['task_id']} · {task['task_type']} · {task['status']} · "
                              f"track={task['track']} vacancy={task['vacancy_id']} · {task['updated_at']}")
                        reason = str((task.get("payload") or {}).get("reason") or "")
                        if reason:
                            print(f"     причина: {reason[:240]}")
                return 0
            if args.workflow_action == "show":
                store, account, track = _campaign_view(
                    config, args.track, profile_override=bool(args.profile),
                    search_override=bool(args.search),
                )
                task = store.workflow_task(args.id, account)
                if task is None or (track and task.get("track") != track):
                    print(f"workflow task not found: {args.id}", file=sys.stderr)
                    return 1
                print(json.dumps(task, ensure_ascii=False, indent=2))
                return 0
            selected_config, track_info = _track_config(
                config, args.track, profile_override=bool(args.profile),
                search_override=bool(args.search),
            )
            search = effective_search(selected_config.load_search())
            profile = _profile(selected_config, search)
            account = _account(profile)
            store = Store(selected_config.db_path)
            if args.workflow_action == "letter-preview":
                item = next((row for row in store.list_campaign_vacancies(
                    account, args.track, "all", limit=1000, source="hh.ru"
                ) if str(row.get("id")) == str(args.id)), None)
                if item is None:
                    print(f"vacancy not found in persistent queue: {args.id}", file=sys.stderr)
                    return 1
                existing = next((task for task in store.workflow_tasks(account, "letter")
                                 if task.get("track") == args.track
                                 and str(task.get("vacancy_id")) == str(args.id)), None)
                if existing and str(existing.get("draft_text") or "").strip():
                    print(f"task_id: {existing['task_id']}; status: {existing['status']}; source: saved")
                    print(existing["draft_text"])
                    return 0
                try:
                    draft, source = _prepare_cover_letter(selected_config, item, profile)
                except (ConfigError, RuntimeError) as exc:
                    print(f"cover letter unavailable: {exc}", file=sys.stderr)
                    return 2 if isinstance(exc, ConfigError) else 3
                if not draft.strip():
                    print("cover letter mode is off or returned an empty draft", file=sys.stderr)
                    return 2
                task_id = store.create_workflow_task(
                    account, "hh.ru", str(args.id), args.track, "letter", "DRAFT",
                    payload={"source": source, "resume": str(item.get("resume") or track_info.get("resume") or ""),
                             "manual_send_required": True},
                    draft_text=draft,
                )
                task = store.workflow_task(task_id, account) or {}
                print(f"task_id: {task_id}; status: {task.get('status', 'DRAFT')}; source: {source}")
                print(task.get("draft_text", draft))
                return 0
            existing = store.workflow_task(args.task_id, account)
            if existing is None or existing.get("track") != args.track:
                print("workflow task not found for this track", file=sys.stderr)
                return 1
            try:
                task = store.update_workflow_task(
                    args.task_id, account,
                    draft_text=args.text if args.workflow_action == "edit" else None,
                    action="edit" if args.workflow_action == "edit" else
                    "approve" if args.workflow_action == "approve" else "sent_confirmed",
                )
            except ValueError as exc:
                print(f"workflow: error ({exc})", file=sys.stderr)
                return 2
            if task.get("track") != args.track:
                print("workflow task belongs to a different track", file=sys.stderr)
                return 2
            print(f"workflow: {task['task_id']} · {task['status']}")
            return 0
        except (ConfigError, ValueError, OSError) as exc:
            print(f"workflow: error ({str(exc)[:240]})", file=sys.stderr)
            return 2
    if args.command == "inbox":
        try:
            profile = _profile(config, effective_search(config.load_search()))
            account = _account(profile)
            store = Store(config.db_path)
            if args.inbox_action == "sync":
                from .inbox import InboxSyncError, sync_hh_inbox

                token = os.getenv("HH_ACCESS_TOKEN", "")
                try:
                    result = sync_hh_inbox(
                        store, token, account, max_chats=args.max_chats,
                        messages_per_chat=args.messages_per_chat,
                    )
                except InboxSyncError as exc:
                    print(f"inbox sync: {exc}", file=sys.stderr)
                    return 2
                print(f"inbox sync: {result['status']}; chats={result['chats_checked']} · "
                      f"received={result['messages_received']} · new={result['messages_saved']} · "
                      f"truncated={result['truncated']}")
                for error in result["errors"][:10]:
                    print(f"  note: {error}")
                return 2 if result["status"] == "partial" else 0
            if not 1 <= args.limit <= 1000:
                print("--limit must be between 1 and 1000", file=sys.stderr)
                return 2
            messages = store.inbox_messages(account, args.thread)[-args.limit:]
            if args.json:
                print(json.dumps(messages, ensure_ascii=False, indent=2))
            else:
                for message in messages:
                    print(f"{message['sent_at']} · {message['sender_type']} · "
                          f"{message['sender_name']} · thread={message['thread_id']} "
                          f"vacancy={message['vacancy_id']}")
                    if message["body"]:
                        print(f"     {message['body']}")
            return 0
        except (ConfigError, ValueError, OSError) as exc:
            print(f"inbox: error ({str(exc)[:240]})", file=sys.stderr)
            return 2
    if args.command == "scan":
        raw_search = config.load_search()
        try:
            search = effective_search(raw_search, args.preset)
        except ConfigError as exc:
            print(f"invalid search configuration: {exc}", file=sys.stderr)
            return 2
        if args.salary_from is not None or args.salary_currency or args.salary_missing:
            search["salary"] = {**search.get("salary", {})}
            if args.salary_from is not None:
                search["salary"]["from"] = args.salary_from
            if args.salary_currency:
                search["salary"]["currency"] = args.salary_currency
            if args.salary_missing:
                search["salary"]["missing"] = args.salary_missing
        if args.experience is not None:
            search["experience"] = {**search.get("experience", {}), "allowed": args.experience}
        if args.work_format is not None:
            search["work_formats"] = args.work_format
        if args.request_budget is not None:
            search["request_budget"] = args.request_budget
        if args.sort_mode is not None:
            search["sort_mode"] = args.sort_mode
        if args.add_query:
            additions = [value.strip() for value in args.add_query if value.strip()]
            if not additions:
                print("at least one --add-query value must be non-empty", file=sys.stderr)
                return 2
            search["additional_queries"] = list(dict.fromkeys([
                *search.get("additional_queries", []), *additions,
            ]))
        try:
            search = validate_search(search)
        except ConfigError as exc:
            print(f"invalid search configuration: {exc}", file=sys.stderr)
            return 2
        try:
            groups = search_groups(search)
        except ConfigError as exc:
            print(f"invalid search group configuration: {exc}", file=sys.stderr)
            return 2
        all_items: list[dict] = []
        segments = []
        group_queries: list[str] = []
        if args.pages is not None and args.pages < 1:
            print("--pages must be positive", file=sys.stderr)
            return 2
        if args.request_budget is not None and args.request_budget < 1:
            print("--request-budget must be positive", file=sys.stderr)
            return 2
        request_budget = int(search["request_budget"])
        page = args.page if args.page is not None else 0
        group_specs = []
        for group in groups:
            queries = [args.query] if args.query else list(group.get("queries", []))
            if args.add_query and not args.query:
                queries = list(dict.fromkeys([*queries, *search["additional_queries"]]))
            group_queries.extend(queries)
            if not queries:
                continue
            areas = args.area if args.area is not None else [int(value) for value in (group.get("areas") or [113])]
            pages = args.pages if args.pages is not None else int(group["max_pages"])
            remote = args.remote if args.remote is not None else bool(group.get("only_remote", False))
            days = args.days if args.days is not None else group.get("days")
            date_from = (datetime.now(UTC) - timedelta(days=int(days))).date().isoformat() if days else None
            group_specs.append({"queries": queries, "areas": areas, "pages": pages,
                                "remote": remote, "date_from": date_from,
                                "name": str(group.get("group_name") or "default")})
        if not group_queries:
            print("search query is required (or configure private/config/search.toml)", file=sys.stderr)
            return 2
        # Сохраняем историческое поведение CLI, пока пользователь явно
        # не запросит свежесть или двухпроходную стратегию. UI/watch могут выбрать
        # эти режимы, не уменьшая скрытно старый relevance-бюджет CLI.
        sort_mode = str(search.get("sort_mode", "relevance"))
        if sort_mode == "relevance":
            sort_budgets = (("relevance", request_budget),)
        elif sort_mode == "newest":
            sort_budgets = (("publication_time", request_budget),)
        else:
            newest_budget = (request_budget + 1) // 2
            sort_budgets = (
                ("publication_time", newest_budget),
                ("relevance", request_budget - newest_budget),
            )
        for order_by, allocated_budget in sort_budgets:
            mode_remaining = allocated_budget
            for spec in group_specs:
                if mode_remaining <= 0:
                    segments.append(ScanSegment(
                        spec["queries"][0], spec["areas"][0], 0, "truncated", 0,
                        "search request budget reached", order_by=order_by))
                    break
                group_items, group_segments = scan_many(
                    spec["queries"], spec["areas"], spec["pages"], spec["remote"],
                    date_from=spec["date_from"], start_page=page, pause_seconds=1.0,
                    request_budget=mode_remaining, order_by=order_by,
                )
                mode_remaining -= sum(segment.requests for segment in group_segments)
                for item in group_items:
                    item["search_group"] = spec["name"]
                all_items.extend(group_items)
                segments.extend(group_segments)
        deduplicated: dict[str, dict] = {}
        for item in all_items:
            vacancy_id = str(item.get("id", ""))
            if not vacancy_id or vacancy_id not in deduplicated:
                deduplicated[vacancy_id] = item
                continue
            existing = deduplicated[vacancy_id]
            existing["query_sources"] = sorted(set(existing.get("query_sources", []))
                                                 | set(item.get("query_sources", [])))
            existing["area_sources"] = sorted(set(existing.get("area_sources", []))
                                                | set(item.get("area_sources", [])))
            existing["search_groups"] = sorted(set(existing.get("search_groups", [existing.get("search_group", "")]))
                                               | {item.get("search_group", "")})
            existing["sort_sources"] = sorted(set(existing.get("sort_sources", []))
                                               | set(item.get("sort_sources", [])))
        all_items = list(deduplicated.values())
        group_by_name = {str(group.get("group_name") or "default"): group for group in groups}
        details_limit = (args.details_limit if args.details_limit is not None
                         else int(search.get("details_limit", 100)))
        ranking_profile = _profile(config, search)
        enrichment_candidates = prioritize_for_enrichment(all_items, ranking_profile, details_limit)
        candidate_ids = {candidate.id for candidate in enrichment_candidates}
        enrichment_items = [item for item in all_items if str(item.get("id", "")) in candidate_ids]
        for item in all_items:
            if str(item.get("id", "")) not in candidate_ids:
                item["description_status"] = "provisional"
        enriched, detail_errors = enrich_items(enrichment_items, details_limit, pause_seconds=1.0)
        # Сохраняем полный результат поиска в snapshot, даже если detail-запрос
        # завершился ошибкой или кандидат provisional. Enrichment только дополняет строки;
        # он не решает, останется ли вакансия доступной для дальнейшего отбора.
        enriched_by_id = {str(item.get("id", "")): item for item in enriched}
        for item in all_items:
            enriched_item = enriched_by_id.get(str(item.get("id", "")))
            if enriched_item is not None:
                item.update(enriched_item)
        for item in all_items:
            group_search = group_by_name.get(str(item.get("search_group", "default")), search)
            decision = evaluate_search_filter(item, group_search)
            item["filter_status"] = decision.status
            item["filter_reasons"] = list(decision.reasons)
            item["filter_fields"] = list(decision.fields)
        errors = [f"{segment.query} area {segment.area}: {segment.error}"
                  for segment in segments if segment.error]
        errors.extend(detail_errors)
        failed = [segment for segment in segments if segment.status in {"failed", "captcha"}]
        truncated = any(segment.status == "truncated" for segment in segments)
        if failed:
            status = "partial"
        elif truncated:
            status = "truncated"
        else:
            status = "ok" if all_items else "empty"
        # Фиксируем первое обнаружение вакансии, чтобы UI отличал
        # новую вакансию от уже найденной в предыдущем scan.
        stamp_first_seen(all_items, config.data_dir / "seen.json")
        path = save_snapshot(all_items, config.snapshots_dir, " | ".join(queries), status,
                             "\n".join(errors), segments, args.preset or search.get("preset"))
        print(f"status: {status}; items: {len(all_items)}; segments: {len(segments)}; snapshot: {path}")
        for segment in segments:
            print(f"segment: query={segment.query!r} area={segment.area} pages={segment.pages} "
                  f"requests={segment.requests} order_by={segment.order_by} "
                  f"status={segment.status} items={segment.items}")
        return 0 if status in {"ok", "empty", "truncated"} else 2
    if args.command in {"plan", "apply"}:
        items = load_items(args.input)
        try:
            search = effective_search(config.load_search(), args.preset)
        except ConfigError as exc:
            print(f"invalid search configuration: {exc}", file=sys.stderr)
            return 2
        store = Store(config.db_path)
        profile = _profile(config, search)
        account = _account(profile)
        limit = args.limit if args.limit is not None else _default_limit(profile)
        if limit < 1:
            print("--limit must be positive", file=sys.stderr)
            return 2
        min_score = args.min_score if args.min_score is not None else int(search.get("min_score", 0))
        blocked_ids = store.blocked_ids(account) | _operator_excluded_ids(config.data_dir)
        selected = _print_candidates(items, profile, limit, min_score,
                                      args.skip_security, blocked_ids, args.rescore)
        if args.command == "plan":
            output = args.output or _default_artifact(config, "plans", "json")
            _write_private_json(output, {
                "schema_version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "account": account,
                "input": str(args.input.resolve()),
                "requested_limit": limit,
                "selected": selected,
            })
            print(f"plan: {output}")
            return 0
        if not args.dry_run and not args.run:
            print("Choose --dry-run or --run", file=sys.stderr)
            return 2
        if args.target_success is not None:
            if args.dry_run:
                print("--target-success requires --run", file=sys.stderr)
                return 2
            if args.target_success < 1 or args.target_success > limit:
                print("--target-success must be positive and no greater than --limit", file=sys.stderr)
                return 2
        run_id = uuid.uuid4().hex
        if args.dry_run:
            return _run_dry(store, selected, run_id, account, args.input, limit)
        return _run_apply(
            config, store, selected, run_id, account, args.input, limit, args.target_success,
            headless_override=(True if args.headless else (False if args.visible else None)),
        )
    if args.command == "inspect":
        ok, detail = validate_state(session_path)
        if not ok:
            print(f"session unavailable: {detail}; run `applypilot login` first", file=sys.stderr)
            return 2
        from .inspection import inspect_items, inspect_resumes
        try:
            if args.resumes:
                if args.page_timeout <= 0:
                    print("--page-timeout must be positive", file=sys.stderr)
                    return 2
                print(json.dumps(inspect_resumes(
                    session_path, timeout_ms=round(args.page_timeout * 1000),
                    data_dir=config.data_dir, profile=config.load_profile(),
                ), ensure_ascii=False, sort_keys=True))
                if args.input is None:
                    return 0
            if args.input is None:
                print("--input is required unless --resumes is used", file=sys.stderr)
                return 2
            if args.limit < 1:
                print("--limit must be positive", file=sys.stderr)
                return 2
            items = load_items(args.input)
            if args.selected:
                try:
                    search = effective_search(config.load_search(), args.preset)
                except ConfigError as exc:
                    print(f"invalid search configuration: {exc}", file=sys.stderr)
                    return 2
                min_score = args.min_score if args.min_score is not None else int(search.get("min_score", 0))
                profile = _profile(config, search)
                items = [candidate.to_dict() for candidate in filter_candidates(
                    items, profile, args.limit, min_score,
                    blocked_ids=Store(config.db_path).blocked_ids(_account(profile)),
                )]
            results = inspect_items(
                session_path, items, args.limit, round(args.page_timeout * 1000),
                data_dir=config.data_dir, profile=config.load_profile(),
            )
        except Exception as exc:  # noqa: BLE001 — read-only ошибка браузера отображается, но не повторяется автоматически
            print(f"inspect: error ({str(exc)[:240]})", file=sys.stderr)
            return 3
        for result in results:
            print(result)
        return 0
    if args.command in {"llm", "letter"}:
        items = load_items(args.input)
        if getattr(args, "llm_action", None) == "rerank":
            if not args.enable:
                print("rerank is disabled by default; pass --enable explicitly", file=sys.stderr)
                return 2
            from .llm import rerank
            try:
                search = effective_search(config.load_search(), None)
            except ConfigError as exc:
                print(f"invalid search configuration: {exc}", file=sys.stderr)
                return 2
            try:
                profile = _profile(config, search)
            except ConfigError as exc:
                print(f"invalid profile: {exc}", file=sys.stderr)
                return 2
            ranked = filter_candidates(items, profile, min(args.limit, 20), int(search.get("min_score", 0)))
            try:
                result, source = rerank([item.to_dict() for item in ranked], profile,
                                        config.data_dir / "llm-cache", args.model,
                                        enabled=True, limit=min(args.limit, 20))
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 3
            print(json.dumps({"source": source, "model": args.model, "items": result},
                             ensure_ascii=False, indent=2))
            return 0
        item = next((x for x in items if str(x.get("id", "")) == args.id), None)
        if not item:
            print(f"vacancy id not found: {args.id}", file=sys.stderr)
            return 1
        try:
            text, source = _prepare_cover_letter(config, item, _profile(config))
        except (ConfigError, RuntimeError) as exc:
            print(f"cover letter unavailable: {exc}", file=sys.stderr)
            return 2 if isinstance(exc, ConfigError) else 3
        print(f"source: {source}\n{text}")
        return 0
    if args.command == "history":
        store = Store(config.db_path)
        account = _account(_profile(config))
        if args.history_action == "import":
            paths = [args.source] if args.source.is_file() else sorted(args.source.rglob("apply_log.csv"))
            reports = [store.import_csv(path, account) for path in paths]
            for source, result in zip(paths, reports, strict=True):
                print(
                    f"import: {source}; logical_rows={result.logical_rows}; "
                    f"physical_data_lines={result.physical_data_lines}; events_added={result.events_added}; "
                    f"already_imported={result.already_imported}; sha256={result.source_sha256}"
                )
        else:
            changed = store.reconcile(args.input, account)
            print(f"reconciled: {changed}; unknown attempts were not retried")
        return 0
    if args.command == "sync":
        sync_config = config
        try:
            if args.track:
                sync_config, _track_info = _track_config(
                    config, args.track, profile_override=bool(args.profile),
                    search_override=bool(args.search),
                )
            sync_search = effective_search(sync_config.load_search())
            sync_profile = _profile(sync_config, sync_search)
        except (ConfigError, ValueError) as exc:
            print(f"sync: error ({str(exc)[:240]})", file=sys.stderr)
            return 2
        sync_session_path = sync_config.data_dir / "hh_session.json"
        ok, detail = validate_state(sync_session_path)
        if not ok:
            print(f"session unavailable: {detail}; run `applypilot login` first", file=sys.stderr)
            return 2
        from .negotiations import SyncError, sync_statuses
        try:
            account = _account(sync_profile)
            if args.pages is not None and args.pages < 1:
                print("--pages must be positive", file=sys.stderr)
                return 2
            sync_store = Store(sync_config.db_path)
            with sync_store.run_lock():
                sync_store.recover_interrupted_runs(account)
                rows = sync_statuses(sync_session_path, sync_store, account=account, max_pages=args.pages)
                reconciled = sync_store.reconcile_unknowns_from_negotiations(account)
                snapshot = sync_store.latest_sync(account)
        except SyncError as exc:
            print(f"sync: error ({exc})", file=sys.stderr)
            return 3
        output = args.output or _default_artifact(sync_config, "reports", "json")
        status = snapshot["status"] if snapshot else "unknown"
        _write_private_json(output, {"account": account, "status": status, "rows": rows,
                                    "reconciled_unknown": reconciled})
        print(f"sync: {status}; {len(rows)} statuses; messages=disabled")
        print(f"reconciled_unknown: {len(reconciled)}"
              + (f" ({','.join(reconciled)})" if reconciled else ""))
        print(f"sync_report: {output}")
        return 0
    if args.command == "analytics":
        account = _account(_profile(config))
        result = report(Store(config.db_path), account)
        output = args.output or _default_artifact(config, "reports", "txt")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result + "\n", encoding="utf-8")
        print(result)
        print(f"analytics_report: {output}")
        return 0
    if args.command == "benchmark":
        try:
            result = run_benchmark(args.suite, args.control_only)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["precision"] >= 0.9 and result["recall"] >= 0.85 else 1
    if args.command == "review":
        search = effective_search(config.load_search(), args.preset)
        output = args.output or config.data_dir / "reports" / "review.html"
        print(f"review: {write_review(load_items(args.input), _profile(config, search), output, args.top)}")
        return 0
    if args.command == "screen":
        raw_items = load_items(args.input)
        retry_previous: dict[str, Any] = {}
        retry_accepted: list[dict[str, Any]] = []
        if args.retry_errors:
            try:
                retry_previous = json.loads(args.output.read_text(encoding="utf-8")) if args.output and args.output.exists() else {}
            except (OSError, json.JSONDecodeError):
                retry_previous = {}
            previous_rows = retry_previous.get("results", []) if isinstance(retry_previous, dict) else []
            failed_ids = {str(row.get("id", "")) for row in previous_rows
                          if isinstance(row, dict) and row.get("verdict") == "ERROR"}
            raw_items = [item for item in raw_items if str(item.get("id", "")) in failed_ids]
            retry_ids = {str(item.get("id", "")) for item in raw_items}
            if not raw_items:
                print("screen: no retryable ERROR rows found in the input", file=sys.stderr)
                return 2
            if args.emit_snapshot and args.emit_snapshot.exists():
                try:
                    previous_snapshot = json.loads(args.emit_snapshot.read_text(encoding="utf-8"))
                    retry_accepted = [item for item in previous_snapshot.get("items", [])
                                      if isinstance(item, dict)
                                      and str(item.get("id", "")) not in retry_ids]
                except (OSError, json.JSONDecodeError):
                    retry_accepted = []
        try:
            search = effective_search(config.load_search(), args.preset)
        except ConfigError as exc:
            print(f"invalid search configuration: {exc}", file=sys.stderr)
            return 2
        profile = _profile(config, search)
        # Переопределения prompt из админки дополняют screen-конфиг кандидата/критериев/зарплаты.
        screen_over = dict(profile.get("screen", {}) or {})
        if args.criteria:
            screen_over["criteria"] = args.criteria
        if args.constraints:
            screen_over["constraints"] = args.constraints
        if args.salary_expectation:
            screen_over["salary_expectation"] = args.salary_expectation
        profile = {**profile, "screen": screen_over}
        account = _account(profile)
        min_score = args.min_score if args.min_score is not None else int(search.get("min_score", 0))
        candidates = filter_candidates(
            raw_items, profile, args.limit, min_score,
            blocked_ids=Store(config.db_path).blocked_ids(account),
        )
        by_id = {str(item.get("id", "")): item for item in raw_items}
        to_screen: list[dict] = []
        for candidate in candidates:
            item = dict(by_id.get(candidate.id, {}))
            item["score"] = candidate.score
            to_screen.append(item)
        screen_cfg = profile.get("screen", {}) or {}
        model = args.model or screen_cfg.get("model") or DEFAULT_MODEL
        base_url = args.base_url or screen_cfg.get("base_url") or DEFAULT_BASE_URL
        concurrency = args.concurrency or int(screen_cfg.get("concurrency", DEFAULT_CONCURRENCY))
        report_path = args.output or _default_artifact(config, "reports", "json")

        def _counts(rows: list[dict]) -> dict[str, int]:
            return {verdict: sum(1 for row in rows if row.get("verdict") == verdict)
                    for verdict in ("FIT", "MAYBE", "SKIP", "ERROR")}

        streamed: list[dict] = []

        def _merge_retry_rows(new_rows: list[dict]) -> list[dict]:
            merged = {str(row.get("id", "")): row for row in
                      (retry_previous.get("results", []) if args.retry_errors else [])
                      if isinstance(row, dict) and row.get("id")}
            merged.update({str(row.get("id", "")): row for row in new_rows if row.get("id")})
            return list(merged.values())

        def _on_result(row: dict, done: int, total: int) -> None:
            streamed.append(row)
            # Строка live-прогресса для admin log и инкрементальная запись отчёта.
            print(f"[{done}/{total}] {row.get('verdict', '?'):5} "
                  f"fit={row.get('fit_score', 0):>3} {row.get('name', '')[:70]}", flush=True)
            report_rows = _merge_retry_rows(streamed)
            _write_private_json(report_path, {"model": model, "track": args.track,
                                              "counts": _counts(report_rows), "results": report_rows,
                                              "progress": {"done": done, "total": total}})

        try:
            results = screen_vacancies(
                to_screen, profile, config.data_dir / "screen-cache",
                model=model, base_url=base_url, track=args.track, concurrency=concurrency,
                api_key=aitunnel_api_key(config.root),
                on_result=_on_result, ledger_path=config.data_dir / "spend.jsonl",
            )
        except ScreenError as exc:
            print(f"screen: error ({exc})", file=sys.stderr)
            return 3
        report_rows = _merge_retry_rows(results)
        counts = _counts(report_rows)
        _write_private_json(report_path, {"model": model, "track": args.track,
                                          "counts": counts, "results": report_rows})
        accepted = {"FIT"} if args.accept == "fit" else {"FIT", "MAYBE"}
        accepted_ids = [row["id"] for row in results if row.get("verdict") in accepted]
        for verdict in ("FIT", "MAYBE", "SKIP", "ERROR"):
            print(f"{verdict}: {counts[verdict]}")
        print(f"screened: {len(results)}; model={model}; track={args.track}")
        print(f"screen_report: {report_path}")
        if args.emit_snapshot:
            accepted_items = [by_id[i] for i in accepted_ids if i in by_id]
            if args.retry_errors:
                accepted_items = retry_accepted + accepted_items
            _write_private_json(args.emit_snapshot, {
                "schema_version": 3, "source": "hh.ru", "status": "ok",
                "query": f"screen:{args.track}", "items": accepted_items,
            })
            print(f"accepted ({args.accept}): {len(accepted_items)} -> {args.emit_snapshot}")
        return 0
    if args.command == "config" and args.config_action == "show":
        raw = config.load_search()
        values = effective_search(raw, args.preset)
        values.pop("profile", None)
        print(json.dumps({"values": values, "origins": search_origins(raw, args.preset)},
                         ensure_ascii=False, indent=2))
        return 0
    if args.command == "templates":
        if args.templates_action == "list":
            print("\n".join(list_templates()))
            return 0
        try:
            print(f"created: {create_template(args.name, args.output)}")
        except (FileExistsError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    return 2


def _run_dry(store: Store, selected: list[dict], run_id: str, account: str,
             input_path: Path, requested_limit: int) -> int:
    """Фиксирует офлайн неизменяемый список кандидатов без создания попыток отправки."""
    with store.run_lock():
        store.start_run(run_id, account, "dry-run", input_path, requested_limit, selected)
        for item in selected:
            note = f"would apply with resume={item.get('resume', '')}"
            store.mark_run_item(run_id, str(item.get("id", "")), "prepared", note)
            print(f"{item.get('id')}: prepared — {note}")
        store.finish_run(run_id, "completed")
    _print_run_summary(store, run_id)
    return 0


def _run_apply(config: AppConfig, store: Store, selected: list[dict], run_id: str,
               account: str, input_path: Path, requested_limit: int,
               target_success: int | None = None,
               headless_override: bool | None = None) -> int:
    """Выполняет явно запрошенные браузерные отклики с аудируемыми условиями остановки."""
    try:
        profile = _profile(config, effective_search(config.load_search(), None))
        letter_enabled = letter_mode(profile) != "off"
    except ConfigError as exc:
        print(f"invalid search configuration: {exc}", file=sys.stderr)
        return 2
    if profile.get("reviewed") is not True:
        print("profile is not reviewed; real submissions require reviewed = true", file=sys.stderr)
        return 2
    if _account(profile) != account:
        print("profile account changed after candidate selection; rerun dry-run", file=sys.stderr)
        return 2
    state_path = config.data_dir / "hh_session.json"
    session = check_session(state_path, data_dir=config.data_dir, profile=profile)
    if session.status != "confirmed":
        print(f"session unavailable: {session.status} ({session.detail}); run `applypilot login` first",
              file=sys.stderr)
        return 2
    try:
        from playwright.sync_api import sync_playwright

        from .autoapply import CAPTCHA_NOTE_PREFIX, apply_one, verify_resume_visibility
        from .browser import browser_settings, launch_context
        from .negotiations import SyncError, sync_statuses
    except ImportError:
        print("browser support missing; install with: pip install -e '.[browser]'", file=sys.stderr)
        return 2
    limits = profile.get("limits", {})
    per_run = int(limits.get("per_run", 100))
    per_day = int(limits.get("per_day", 300))
    if per_run < 1 or per_day < 1:
        print("profile limits must be positive", file=sys.stderr)
        return 2
    timing = profile.get("apply", {}) or {}
    visibility_resume_id = str(timing.get("visibility_resume_id") or "").strip()
    visibility_resume_title = str(timing.get("visibility_resume_title") or "").strip()
    if visibility_resume_id and (
        not visibility_resume_title
        or any(str(item.get("resume", "")).strip() != visibility_resume_title for item in selected)
    ):
        print("visibility verification configuration does not match selected resume", file=sys.stderr)
        return 2
    delay_min = max(0.0, float(timing.get("delay_min_seconds", 1)))
    delay_max = max(delay_min, float(timing.get("delay_max_seconds", delay_min)))
    # Плавный темп: иногда добавляем длинные паузы, чтобы не создавать чрезмерную частоту действий.
    long_pause_every = int(timing.get("long_pause_every", 0))
    long_pause_min = max(0.0, float(timing.get("long_pause_min_seconds", 0)))
    long_pause_max = max(long_pause_min, float(timing.get("long_pause_max_seconds", long_pause_min)))
    pacing_rng = random.Random()
    confirmation_timeout = max(0.0, float(timing.get("confirmation_timeout_seconds", 15)))
    browser_options = browser_settings(profile)
    if headless_override is not None:
        browser_options["headless"] = headless_override
    run_headless = bool(browser_options["headless"])
    manual_captcha_timeout = float(browser_options["manual_captcha_timeout_seconds"])
    interactive_captcha = (
        not run_headless
        and (sys.stdin.isatty() or os.getenv("APPLYPILOT_INTERACTIVE", "") == "1")
    )
    run_status = "completed"
    stop_reason = ""
    exit_code = 0
    confirmed_successes = 0
    with store.run_lock():
        store.recover_interrupted_runs(account)
        store.start_run(run_id, account, "apply", input_path, requested_limit, selected)
        browser_session = None
        context = None
        playwright = None
        try:
            playwright = sync_playwright().start()
            browser_session = launch_context(
                playwright,
                data_dir=config.data_dir,
                state_path=state_path,
                profile=profile,
                headless=run_headless,
            )
            context = browser_session.context
            page = context.new_page()
            # Some HH popups retain a visibility warning even while the exact
            # resume page confirms public access. Verify it again in this same
            # browser context before allowing any attempt past the banner.
            visibility_verified = False
            if visibility_resume_id:
                visibility_verified = verify_resume_visibility(
                    context, visibility_resume_id, visibility_resume_title
                )
                if not visibility_verified:
                    raise RuntimeError("HH resume visibility could not be independently verified")
            for index, item in enumerate(selected, 1):
                vacancy_id = str(item.get("id", ""))
                if vacancy_id in _operator_excluded_ids(config.data_dir):
                    reason = "manually marked viewed, bad, or applied in admin"
                    store.mark_run_item(run_id, vacancy_id, "skipped", reason)
                    print(f"{vacancy_id}: skipped — {reason}")
                    continue
                approved_letter = str(item.get("approved_cover_letter") or "").strip()
                campaign_queue_item = bool(item.get("track"))
                requires_letter = any(item.get(key) for key in (
                    "cover_letter_required", "requires_cover_letter", "letter_required",
                ))
                if campaign_queue_item and requires_letter and not approved_letter:
                    note = "required cover letter needs an owner-approved saved draft"
                    store.record(item, "needs_manual", note, run_id, account)
                    print(f"{item.get('id')}: needs_manual — {note}")
                    continue
                item_letter_enabled = (bool(approved_letter) if campaign_queue_item
                                       else letter_enabled or bool(approved_letter))
                preflight_error = _submission_preflight(item, item_letter_enabled)
                if preflight_error:
                    store.record(item, "needs_manual", preflight_error, run_id, account)
                    print(f"{item.get('id')}: needs_manual — {preflight_error}")
                    continue
                allowed, reason = store.reserve(item, run_id, per_run, per_day, account)
                if not allowed:
                    store.mark_run_item(run_id, str(item.get("id", "")), "skipped", reason)
                    print(f"{item.get('id')}: skipped — {reason}")
                    if reason.startswith(("run budget", "daily budget")):
                        run_status = "budget_exhausted"
                        stop_reason = reason
                        break
                    continue
                cover_letter = approved_letter
                if approved_letter:
                    item["cover_letter_source"] = "owner_approved"
                elif letter_enabled and not campaign_queue_item:
                    try:
                        cover_letter, source = _prepare_cover_letter(config, item, profile)
                    except (Exception, KeyboardInterrupt):
                        store.record(item, "failed_before_submit", "cover letter generation failed",
                                     run_id, account)
                        raise
                    item["cover_letter_source"] = source
                    if source in {"generated", "cache"}:
                        item["llm_source"] = source
                if cover_letter:
                    # Store.record не сохраняет тело письма в журнал попыток; оно уже
                    # хранится в приватной workflow_tasks вместе с версией/одобрением.
                    item["cover_letter"] = cover_letter
                result = apply_one(
                    page, item, str(item.get("resume", "")).strip(), cover_letter,
                    confirmation_timeout_seconds=confirmation_timeout,
                    visibility_verified=visibility_verified,
                    manual_captcha_timeout=manual_captcha_timeout,
                    interactive=interactive_captcha,
                )
                if result.status == "unknown":
                    store.record(item, result.status, result.note, run_id, account)
                    if run_headless and "CAPTCHA" in result.note:
                        print(
                            "CAPTCHA in headless mode: solve it in visible Chrome, then run "
                            "`sync`; do not repeat this vacancy automatically",
                            file=sys.stderr,
                        )
                    try:
                        save_state(state_path, context.storage_state())
                        sync_statuses(state_path, store, account=account)
                        reconciled = store.reconcile_unknowns_from_negotiations(account)
                    except SyncError as exc:
                        LOGGER.warning("Could not confirm vacancy %s through sync: %s", item.get("id"), exc)
                        reconciled = []
                    vacancy_id = str(item.get("id", ""))
                    if vacancy_id in reconciled:
                        print(f"{vacancy_id}: success — confirmed by HH negotiations")
                        confirmed_successes += 1
                    else:
                        print(f"{vacancy_id}: unknown — {result.note}")
                        run_status = "stopped_unknown"
                        stop_reason = f"unknown result for vacancy {vacancy_id}"
                        exit_code = 3
                        break
                else:
                    store.record(item, result.status, result.note, run_id, account)
                    print(f"{item.get('id')}: {result.status} — {result.note}")
                    if result.status == "success":
                        confirmed_successes += 1
                    if result.status == "needs_manual" and result.note.startswith(CAPTCHA_NOTE_PREFIX):
                        run_status = "stopped_captcha"
                        stop_reason = (
                            f"manual HH CAPTCHA required before submission for {item.get('id')}; "
                            "queue preserved"
                        )
                        if run_headless:
                            stop_reason += "; rerun with --visible to solve it in Chrome"
                        exit_code = 5
                        break
                if target_success is not None and confirmed_successes >= target_success:
                    run_status = "target_reached"
                    stop_reason = f"confirmed success target reached ({confirmed_successes})"
                    break
                if index < len(selected) and delay_max:
                    time.sleep(next_delay(
                        pacing_rng, min_seconds=delay_min, max_seconds=delay_max, index=index,
                        long_pause_every=long_pause_every,
                        long_pause_min=long_pause_min, long_pause_max=long_pause_max,
                    ))
            if confirmed_successes and any(item.get("track") for item in selected):
                try:
                    save_state(state_path, context.storage_state())
                    synced = sync_statuses(state_path, store, account=account)
                    selected_ids = {str(item.get("id") or "") for item in selected}
                    negotiation_ids = {
                        str(row.get("vacancy_id") or row.get("id") or "") for row in synced
                    } & selected_ids
                    print(f"post-apply HH negotiation sync: {len(negotiation_ids)} of "
                          f"{confirmed_successes} confirmed success(es) found")
                except Exception as exc:  # noqa: BLE001 — UI confirmation remains auditable if sync is unavailable
                    LOGGER.warning("Post-apply HH negotiation sync failed (%s)", type(exc).__name__)
                    print(f"post-apply HH negotiation sync unavailable ({type(exc).__name__})",
                          file=sys.stderr)
        except KeyboardInterrupt:
            run_status = "interrupted"
            stop_reason = "run interrupted"
            exit_code = 130
        except Exception as exc:  # noqa: BLE001 — журнал запуска должен закрыться даже при ошибке настройки браузера
            run_status = "failed"
            stop_reason = f"runner error: {str(exc)[:160]}"
            exit_code = 3
            print(stop_reason, file=sys.stderr)
        finally:
            try:
                if context is not None:
                    save_state(state_path, context.storage_state())
            except Exception as exc:  # noqa: BLE001 — сохранение сессии не должно оставлять run незавершённым
                if run_status == "completed":
                    run_status = "failed"
                    stop_reason = f"session persistence error: {str(exc)[:160]}"
                    exit_code = 3
                    print(stop_reason, file=sys.stderr)
            finally:
                if browser_session is not None:
                    try:
                        browser_session.close()
                    except Exception:
                        LOGGER.debug("Could not close browser session", exc_info=True)
                elif context is not None:
                    try:
                        context.close()
                    except Exception:
                        LOGGER.debug("Could not close browser context", exc_info=True)
                if playwright is not None:
                    try:
                        playwright.stop()
                    except Exception:
                        LOGGER.debug("Could not stop Playwright", exc_info=True)
            if target_success is not None and run_status == "completed":
                run_status = "completed_shortfall"
                stop_reason = (
                    f"confirmed success target not reached ({confirmed_successes}/{target_success})"
                )
                exit_code = 4
            store.finish_run(run_id, run_status, stop_reason)
            store.recover_interrupted_runs(account)
    _print_run_summary(store, run_id)
    return exit_code


def _prepare_cover_letter(config: AppConfig, item: dict, profile: dict) -> tuple[str, str]:
    """Готовит одно и то же письмо для preview и apply без скрытого включения fallback."""
    mode = letter_mode(profile)
    if mode == "off":
        return "", "disabled"
    prepared = load_letter_profile(profile, config.profile_path)
    vacancy = dict(item)
    if not vacancy.get("resume"):
        vacancy["resume"] = choose_resume(str(vacancy.get("name", "")), prepared)
    if mode == "template":
        return render_template(vacancy, prepared), "template"
    settings = prepared.get("cover_letter", {})
    fallback = render_template(vacancy, prepared) if settings.get("fallback_to_template", False) else None
    provider = str(settings.get("provider", "openrouter") or "openrouter").strip()
    if provider not in ("openrouter", "aitunnel"):
        raise ConfigError("cover_letter.provider must be openrouter or aitunnel")
    try:
        if provider == "aitunnel":
            # Используем тот же генератор и prompt для вакансии, что и web-admin (letters.py),
            # поэтому отправляется именно тот тип письма, который пользователь предварительно проверял.
            screen = prepared.get("screen", {}) if isinstance(prepared.get("screen"), dict) else {}
            model = str(settings.get("model") or screen.get("model") or letters.DEFAULT_MODEL).strip()
            base_url = str(settings.get("base_url") or screen.get("base_url")
                           or letters.DEFAULT_BASE_URL).strip()
            result = letters.generate_letter(
                vacancy,
                prepared,
                config.data_dir / "letter-cache",
                model=model,
                base_url=base_url,
                api_key=aitunnel_api_key(config.root),
            )
            text, source = str(result.get("text", "")), str(result.get("source", "generated"))
        else:
            llm = prepared.get("llm", {})
            if not isinstance(llm, dict) or not isinstance(llm.get("model", ""), str):
                raise ConfigError("llm.model must be a string")
            text, source = generate(vacancy, prepared, config.data_dir / "llm-cache",
                                    str(llm.get("model", "")), enabled=True, required=True)
        if not text.strip():
            raise RuntimeError("provider returned an empty cover letter")
        return text, source
    except RuntimeError:
        if fallback is not None:
            return fallback, "template_fallback"
        raise


def _submission_preflight(item: dict, letter_enabled: bool) -> str:
    """Возвращает причину ручной проверки до создания apply-reservation."""
    from .autoapply import allowed_hh_url

    if not str(item.get("resume", "")).strip():
        return "resume is not selected"
    if not allowed_hh_url(str(item.get("url", ""))):
        return "URL is not an allowed HH hostname"
    requires_letter = any(item.get(key) for key in (
        "cover_letter_required", "requires_cover_letter", "letter_required",
    ))
    if requires_letter and not letter_enabled:
        return "required cover letter is missing"
    return ""
