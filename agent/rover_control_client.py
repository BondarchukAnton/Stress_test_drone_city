#!/usr/bin/env python3
"""Small dependency-free client for the rover HTTP control API."""

from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
import json
import math
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


class RoverApiError(RuntimeError):
    pass


class RoverClient:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        lease_id: str = '',
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode('utf-8')
        headers = {'Accept': 'application/json'}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        if lease_id:
            headers['X-Control-Lease'] = lease_id
        request = Request(
            f'{self.base_url}{path}',
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode('utf-8'))
                message = f'{exc.code} {payload.get("code")}: {payload.get("message")}'
            except Exception:
                message = f'HTTP {exc.code}: {exc.reason}'
            raise RoverApiError(message) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RoverApiError(f'cannot reach {self.base_url}: {exc}') from exc

    def get(self, path: str) -> dict[str, Any]:
        return self.request('GET', path)

    def post(
        self,
        path: str,
        body: dict[str, Any],
        lease_id: str = '',
    ) -> dict[str, Any]:
        return self.request('POST', path, body, lease_id)


class ControlLease(AbstractContextManager):
    def __init__(self, client: RoverClient, client_id: str) -> None:
        self.client = client
        self.client_id = client_id
        self.lease_id = ''
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._renew_error: BaseException | None = None

    def __enter__(self) -> 'ControlLease':
        response = self.client.post(
            '/v1/lease/acquire',
            {'client_id': self.client_id},
        )
        self.lease_id = str(response['lease_id'])
        self._thread = threading.Thread(target=self._renew_loop, daemon=True)
        self._thread.start()
        return self

    def _renew_loop(self) -> None:
        while not self._stop.wait(2.0):
            try:
                self.client.post('/v1/lease/renew', {}, self.lease_id)
            except BaseException as exc:
                self._renew_error = exc
                self._stop.set()

    def check(self) -> None:
        if self._renew_error is not None:
            raise RoverApiError(f'control lease renewal failed: {self._renew_error}')

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self.lease_id:
            try:
                self.client.post('/v1/lease/release', {}, self.lease_id)
            except RoverApiError:
                pass


def request_id() -> str:
    return str(uuid.uuid4())


def pretty(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def calibrated_cell(
    client: RoverClient,
    column: int,
    row: int,
    yaw_field_deg: float = 0.0,
) -> dict[str, Any]:
    response = client.get('/v1/field-calibration')
    calibration = response.get('calibration') if response.get('configured') else None
    if not isinstance(calibration, dict):
        raise RoverApiError('центр поля не задан в веб-интерфейсе')
    columns = int(calibration.get('columns', 6))
    rows = int(calibration.get('rows', 6))
    if not 1 <= column <= columns or not 1 <= row <= rows:
        raise RoverApiError(f'клетка должна быть в диапазоне 1..{columns} × 1..{rows}')
    # The four-corner overlay contains both orientation and field scale.
    corners = calibration.get('corners')
    if not isinstance(corners, dict):
        raise RoverApiError('сетка поля не задана в веб-интерфейсе')
    u = (column - 0.5) / columns
    v = (row - 0.5) / rows
    bl, br = corners['bottom_left'], corners['bottom_right']
    tr, tl = corners['top_right'], corners['top_left']
    x = (
        (1 - u) * (1 - v) * float(bl['x'])
        + u * (1 - v) * float(br['x'])
        + u * v * float(tr['x'])
        + (1 - u) * v * float(tl['x'])
    )
    y = (
        (1 - u) * (1 - v) * float(bl['y'])
        + u * (1 - v) * float(br['y'])
        + u * v * float(tr['y'])
        + (1 - u) * v * float(tl['y'])
    )
    dx = (1 - v) * (float(br['x']) - float(bl['x'])) + v * (float(tr['x']) - float(tl['x']))
    dy = (1 - v) * (float(br['y']) - float(bl['y'])) + v * (float(tr['y']) - float(tl['y']))
    grid_yaw_deg = math.degrees(math.atan2(dy, dx))
    return {
        'map_label': str(calibration['map_label']),
        'x': x,
        'y': y,
        'yaw_deg': grid_yaw_deg + yaw_field_deg,
        'column': column,
        'row': row,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Control a SVERK rover over HTTP')
    parser.add_argument('--url', default='http://192.168.4.9:8767')
    parser.add_argument('--client-id', default=f'mac-{uuid.uuid4().hex[:8]}')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('state')
    sub.add_parser('nav-status')
    sub.add_parser('field-status')

    cell = sub.add_parser('cell')
    cell.add_argument('column', type=int)
    cell.add_argument('row', type=int)
    cell.add_argument('--yaw', type=float, default=0.0)

    initial = sub.add_parser('initial-pose')
    initial.add_argument('--map', required=True)
    initial.add_argument('x', type=float)
    initial.add_argument('y', type=float)
    initial.add_argument('--yaw', type=float, default=0.0)

    initial_cell = sub.add_parser('initial-cell')
    initial_cell.add_argument('column', type=int)
    initial_cell.add_argument('row', type=int)
    initial_cell.add_argument('--yaw', type=float, default=0.0)

    goal = sub.add_parser('goal')
    goal.add_argument('--map', required=True)
    goal.add_argument('x', type=float)
    goal.add_argument('y', type=float)
    goal.add_argument('--yaw', type=float, default=0.0)
    goal.add_argument('--replace', action='store_true')
    goal.add_argument('--wait-timeout', type=float, default=300.0)

    goal_cell = sub.add_parser('goal-cell')
    goal_cell.add_argument('column', type=int)
    goal_cell.add_argument('row', type=int)
    goal_cell.add_argument('--yaw', type=float, default=0.0)
    goal_cell.add_argument('--replace', action='store_true')
    goal_cell.add_argument('--wait-timeout', type=float, default=300.0)

    sub.add_parser('cancel')

    teleop = sub.add_parser('teleop')
    teleop.add_argument('--linear-x', type=float, default=0.0)
    teleop.add_argument('--linear-y', type=float, default=0.0)
    teleop.add_argument('--angular-z', type=float, default=0.0)
    teleop.add_argument('--duration', type=float, default=1.0)

    stop = sub.add_parser('stop')
    stop.add_argument('--reason', default='operator requested stop')
    sub.add_parser('clear')
    return parser


def main() -> int:
    args = build_parser().parse_args()
    client = RoverClient(args.url)

    if args.command == 'state':
        pretty(client.get('/v1/state'))
        return 0
    if args.command == 'nav-status':
        pretty(client.get('/v1/navigation/status'))
        return 0
    if args.command == 'field-status':
        pretty(client.get('/v1/field-calibration'))
        return 0
    if args.command == 'cell':
        pretty(calibrated_cell(client, args.column, args.row, args.yaw))
        return 0
    if args.command == 'stop':
        pretty(client.post('/v1/stop', {'reason': args.reason}))
        return 0

    with ControlLease(client, args.client_id) as lease:
        if args.command == 'clear':
            pretty(client.post('/v1/stop/clear', {}, lease.lease_id))
            return 0
        if args.command == 'initial-pose':
            pretty(client.post(
                '/v1/localization/initial-pose',
                {
                    'request_id': request_id(),
                    'map_label': args.map,
                    'x': args.x,
                    'y': args.y,
                    'yaw_deg': args.yaw,
                },
                lease.lease_id,
            ))
            return 0
        if args.command == 'initial-cell':
            cell_pose = calibrated_cell(client, args.column, args.row, args.yaw)
            pretty(client.post(
                '/v1/localization/initial-pose',
                {
                    'request_id': request_id(),
                    'map_label': cell_pose['map_label'],
                    'x': cell_pose['x'],
                    'y': cell_pose['y'],
                    'yaw_deg': cell_pose['yaw_deg'],
                },
                lease.lease_id,
            ))
            return 0
        if args.command == 'cancel':
            pretty(client.post(
                '/v1/navigation/cancel',
                {'request_id': request_id()},
                lease.lease_id,
            ))
            return 0
        if args.command == 'teleop':
            deadline = time.monotonic() + max(0.0, args.duration)
            seq = 0
            while time.monotonic() < deadline:
                lease.check()
                client.post(
                    '/v1/teleop',
                    {
                        'request_id': request_id(),
                        'seq': seq,
                        'linear_x': args.linear_x,
                        'linear_y': args.linear_y,
                        'angular_z': args.angular_z,
                        'ttl_ms': 300,
                    },
                    lease.lease_id,
                )
                seq += 1
                time.sleep(0.1)
            pretty(client.post(
                '/v1/teleop',
                {
                    'request_id': request_id(),
                    'seq': seq,
                    'linear_x': 0.0,
                    'linear_y': 0.0,
                    'angular_z': 0.0,
                    'ttl_ms': 100,
                },
                lease.lease_id,
            ))
            return 0
        if args.command in {'goal', 'goal-cell'}:
            if args.command == 'goal-cell':
                cell_pose = calibrated_cell(client, args.column, args.row, args.yaw)
                map_label = cell_pose['map_label']
                goal_x = cell_pose['x']
                goal_y = cell_pose['y']
                goal_yaw = cell_pose['yaw_deg']
            else:
                map_label = args.map
                goal_x = args.x
                goal_y = args.y
                goal_yaw = args.yaw
            response = client.post(
                '/v1/navigation/goal',
                {
                    'request_id': request_id(),
                    'map_label': map_label,
                    'x': goal_x,
                    'y': goal_y,
                    'yaw_deg': goal_yaw,
                    'replace_active': args.replace,
                },
                lease.lease_id,
            )
            pretty(response)
            deadline = time.monotonic() + args.wait_timeout
            terminal = {'succeeded', 'aborted', 'canceled', 'rejected', 'error'}
            while time.monotonic() < deadline:
                lease.check()
                status = client.get('/v1/navigation/status')
                print(
                    f"{status.get('state')}: {status.get('message')} "
                    f"distance={status.get('distance_remaining')}"
                )
                if status.get('state') in terminal:
                    return 0 if status.get('state') == 'succeeded' else 2
                time.sleep(0.5)
            raise RoverApiError('timed out waiting for navigation result')

    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except RoverApiError as exc:
        print(f'ERROR: {exc}')
        raise SystemExit(1)
