"""Read-only MVP1 product endpoints."""
import json
import sqlite3
import zipfile
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from . import doctor, reports, settings, store


def create_router(dataset_loader):
    router = APIRouter()

    @router.get('/api/system')
    def system():
        return doctor.snapshot(dataset_loader)

    def report_for(job_id):
        if not settings.DB.is_file():
            raise HTTPException(404, 'Experiment not found')
        try:
            with closing(doctor.read_connection(settings.DB)) as conn:
                conn.row_factory = sqlite3.Row
                job = store.decode(conn.execute('SELECT * FROM experiments WHERE id=?', (job_id,)).fetchone())
            if job is None:
                raise HTTPException(404, 'Experiment not found')
            return reports.build(job)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError,
                sqlite3.Error, zipfile.BadZipFile, RuntimeError):
            raise HTTPException(409, 'Historical evidence is unavailable, invalid or inconsistent; report cannot be generated') from None

    @router.get('/api/experiments/{job_id}/report')
    def report(job_id: str):
        return report_for(job_id)

    @router.get('/api/experiments/{job_id}/report/download')
    def download(job_id: str, format: Literal['html', 'json'] = 'html'):
        value = report_for(job_id)
        content = reports.html(value) if format == 'html' else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        return Response(content=content, media_type='text/html' if format == 'html' else 'application/json',
                        headers={'Content-Disposition': f'attachment; filename="quantpilot-report.{format}"',
                                 'X-Content-Type-Options': 'nosniff',
                                 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'"})

    return router
