"""Service settings, all from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    dsn: str  # FW_DSN, e.g. postgresql://fw:<password>@db:5432/fw
    store: Path  # FW_STORE: root of the file store
    host: str = '0.0.0.0'  # FW_HOST
    port: int = 8000  # FW_PORT
    workers: int = 4  # FW_WORKERS: server processes
    pool_size: int = 8  # FW_POOL_SIZE: database connections per process
    max_object_bytes: int = 64 * 1024 ** 3  # FW_MAX_OBJECT_GB
    job_lease_seconds: int = 60  # FW_JOB_LEASE_SECONDS: a worker that stops renewing loses its job after this

    @classmethod
    def from_env(cls) -> 'Settings':
        missing = [k for k in ('FW_DSN', 'FW_STORE') if not os.environ.get(k)]
        if missing:
            raise SettingsError(f'需要环境变量 {"、".join(missing)}')
        env = os.environ
        return cls(dsn=env['FW_DSN'], store=Path(env['FW_STORE']), host=env.get('FW_HOST', '0.0.0.0'),
                   port=int(env.get('FW_PORT', 8000)), workers=int(env.get('FW_WORKERS', 4)),
                   pool_size=int(env.get('FW_POOL_SIZE', 8)),
                   max_object_bytes=int(float(env.get('FW_MAX_OBJECT_GB', 64)) * 1024 ** 3),
                   job_lease_seconds=int(env.get('FW_JOB_LEASE_SECONDS', 60)))
