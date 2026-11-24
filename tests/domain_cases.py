from app.catalog import SPEC
from app.domain.calculate import calculate_rows
from app.domain.decision import decide_rows
from app.domain.prepare import prepare_rows
from app.intake.service import load

CLEAN = [{'waybill_id': 'W1',
  'pickup_at': '2026-11-06 18:00',
  'deadline_at': '2026-11-08 18:00',
  'delivered_at': '2026-11-09 20:00',
  'paused_hours': '2',
  'declared_value_cents': '10000',
  'invoice_cents': '0'},
 {'waybill_id': 'W2',
  'pickup_at': '2026-11-09 08:00',
  'deadline_at': '2026-11-09 12:00',
  'delivered_at': '2026-11-09 18:00',
  'paused_hours': '2',
  'declared_value_cents': '10000',
  'invoice_cents': '500'},
 {'waybill_id': 'W3',
  'pickup_at': '2026-11-10 08:00',
  'deadline_at': '2026-11-11 09:00',
  'delivered_at': '2026-11-11 15:00',
  'paused_hours': '0',
  'declared_value_cents': '20000',
  'invoice_cents': '2000'},
 {'waybill_id': 'W4',
  'pickup_at': '2026-09-30 08:00',
  'deadline_at': '2026-10-08 08:00',
  'delivered_at': '2026-10-08 18:00',
  'paused_hours': '0',
  'declared_value_cents': '30000',
  'invoice_cents': '3000'},
 {'waybill_id': 'W5',
  'pickup_at': '2026-11-09 08:00',
  'deadline_at': '2026-11-09 08:00',
  'delivered_at': '2026-11-12 18:00',
  'paused_hours': '0',
  'declared_value_cents': '5000',
  'invoice_cents': '1000'}]


def row(**changes):
    item = dict(CLEAN[0])
    item.update(changes)
    return item


def calc(rows):
    items, issues = prepare_rows(rows)
    return calculate_rows(items), issues


def run(rows):
    items, issues = prepare_rows(rows)
    return decide_rows(calculate_rows(items)), issues


def run_profile(profile):
    return run(load(profile)["rows"])
