# src/metrics.py
# Centralized Prometheus metrics

from prometheus_client import Counter, Gauge, Histogram

# Bot lifecycle
BOT_UP = Gauge('api_monitor_bot_up', 'Bot up (1)')

# Monitoring metrics (labeled by api_id)
CHECKS_TOTAL = Counter('api_monitor_checks_total', 'Total checks performed', ['api_id'])
CHECKS_FAIL = Counter('api_monitor_checks_fail_total', 'Failed checks', ['api_id'])
INCIDENTS_TOTAL = Counter('api_monitor_incidents_total', 'Incidents started', ['api_id'])
ANOMALIES_TOTAL = Counter('api_monitor_anomalies_total', 'Anomalies detected', ['api_id'])

# Response time distribution (ms)
RESPONSE_TIME_MS = Histogram(
    'api_monitor_response_time_ms',
    'API response time in milliseconds',
    ['api_id'],
    buckets=[25, 50, 75, 100, 150, 200, 300, 500, 700, 1000, 1500, 2000, 3000, 5000, 10000]
)

# ML metric gauges (per API)
ML_MEDIAN_MS = Gauge('api_monitor_ml_median_ms', 'ML median response time (ms)', ['api_id'])
ML_MAD_MS = Gauge('api_monitor_ml_mad_ms', 'ML MAD (ms)', ['api_id'])
ML_UCL_MS = Gauge('api_monitor_ml_ucl_ms', 'ML UCL threshold (ms)', ['api_id'])
ML_P95_MS = Gauge('api_monitor_ml_p95_ms', 'Recent P95 (ms)', ['api_id'])
