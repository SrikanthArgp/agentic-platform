# The upstream collector image is distroless (no shell, no wget), so a
# Compose healthcheck can't run inside it. Re-home the static binary onto
# busybox so `wget` can probe the health_check extension.
FROM otel/opentelemetry-collector-contrib:0.111.0 AS otelcol

FROM busybox:1.36
COPY --from=otelcol /otelcol-contrib /otelcol-contrib
ENTRYPOINT ["/otelcol-contrib"]
CMD ["--config", "/etc/otelcol-contrib/config.yaml"]
