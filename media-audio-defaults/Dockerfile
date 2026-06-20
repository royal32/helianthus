FROM alpine:3.22

RUN apk add --no-cache bash coreutils findutils inotify-tools jq mkvtoolnix

COPY normalize-audio-defaults.sh /usr/local/bin/normalize-audio-defaults

ENTRYPOINT ["/usr/local/bin/normalize-audio-defaults"]
