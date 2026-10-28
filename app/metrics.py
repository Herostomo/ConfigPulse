from prometheus_client import Counter, Gauge, Histogram

CHANGES_DETECTED = Counter("config_changes_detected_total", "Configuration change events created", ["event_type"])
DELIVERY_ATTEMPTS = Counter("webhook_delivery_attempts_total", "Webhook delivery attempts", ["outcome"])
PARSE_ERRORS = Counter("config_parse_errors_total", "Configuration parse/read errors")
PROCESSING_SECONDS = Histogram("config_processing_duration_seconds", "Time spent reading/parsing/diffing a monitored file")
DELIVERY_SECONDS = Histogram("webhook_delivery_duration_seconds", "Time spent on webhook delivery attempts")
PENDING_JOBS = Gauge("delivery_jobs_pending", "Number of pending delivery jobs")
DEAD_LETTER_JOBS = Gauge("delivery_jobs_dead_letter", "Number of dead-letter delivery jobs")
