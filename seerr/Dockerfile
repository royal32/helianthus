ARG SEERR_REF=v3.3.0
FROM ghcr.io/seerr-team/seerr:${SEERR_REF}

COPY --chown=node:node patch-search.mjs /tmp/patch-search.mjs
RUN node /tmp/patch-search.mjs && rm /tmp/patch-search.mjs
