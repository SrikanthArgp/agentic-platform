# Same reason as otel-collector/Dockerfile: upstream Mimir is distroless, so
# re-home the static binary onto busybox for a wget-based healthcheck.
FROM grafana/mimir:2.14.0 AS mimir

FROM busybox:1.36
COPY --from=mimir /bin/mimir /bin/mimir
ENTRYPOINT ["/bin/mimir"]
CMD ["-config.file=/etc/mimir/mimir.yaml"]
