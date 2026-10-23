"""Read-only public market endpoints used by the personal terminal."""
from fastapi import APIRouter, HTTPException
from . import terminal_market

router = APIRouter(prefix='/api/terminal')


@router.get('/markets')
def markets(refresh: bool = False):
    return terminal_market.markets(refresh)


@router.get('/market')
def market(symbol: str = 'BTC/USDT', refresh: bool = False):
    try:
        return terminal_market.snapshot(symbol, refresh)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
