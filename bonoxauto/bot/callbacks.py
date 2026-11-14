"""Данные inline-кнопок (не длиннее 64 байт)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class TaskCb(CallbackData, prefix="t"):
    action: str
    task_id: int = 0
    #: Для подтверждения «купить сейчас»: момент показа кнопки в мс. Служит ключом
    #: идемпотентности — двойное нажатие той же кнопки не создаст второй заказ.
    stamp: int = 0


class WizardCb(CallbackData, prefix="w"):
    action: str
    value: str = ""


class ModeCb(CallbackData, prefix="m"):
    action: str
