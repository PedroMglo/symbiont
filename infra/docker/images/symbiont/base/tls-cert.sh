#!/bin/sh
# Generate a local ai-local CA and per-service TLS certificate.
set -eu

ai_local_tls_log() {
    echo "INFO: $*" >&2
}

ai_local_tls_fatal() {
    echo "FATAL: $*" >&2
    exit 1
}

ai_local_tls_lock() {
    _lock_dir="${AI_LOCAL_TLS_DIR}/.lock"
    _lock_wait=0
    while ! mkdir "$_lock_dir" 2>/dev/null; do
        _lock_wait=$((_lock_wait + 1))
        if [ "$_lock_wait" -ge 30 ]; then
            ai_local_tls_fatal "timed out waiting for TLS certificate lock: $_lock_dir"
        fi
        sleep 1
    done
}

ai_local_tls_unlock() {
    rmdir "${AI_LOCAL_TLS_DIR}/.lock" 2>/dev/null || true
}

ai_local_tls_add_dns_name() {
    _name="$1"
    [ -n "$_name" ] || return 0
    case " $AI_LOCAL_TLS_DNS_SEEN " in
        *" $_name "*) return 0 ;;
    esac
    AI_LOCAL_TLS_DNS_SEEN="${AI_LOCAL_TLS_DNS_SEEN} ${_name}"
    AI_LOCAL_TLS_DNS_COUNT=$((AI_LOCAL_TLS_DNS_COUNT + 1))
    printf 'DNS.%s = %s\n' "$AI_LOCAL_TLS_DNS_COUNT" "$_name" >> "$AI_LOCAL_TLS_EXT_FILE"
}

ai_local_tls_add_ip_address() {
    _ip="$1"
    [ -n "$_ip" ] || return 0
    case " $AI_LOCAL_TLS_IP_SEEN " in
        *" $_ip "*) return 0 ;;
    esac
    AI_LOCAL_TLS_IP_SEEN="${AI_LOCAL_TLS_IP_SEEN} ${_ip}"
    AI_LOCAL_TLS_IP_COUNT=$((AI_LOCAL_TLS_IP_COUNT + 1))
    printf 'IP.%s = %s\n' "$AI_LOCAL_TLS_IP_COUNT" "$_ip" >> "$AI_LOCAL_TLS_EXT_FILE"
}

ai_local_tls_generate_ca() {
    if [ -s "$AI_LOCAL_TLS_CA_CERT_FILE" ] && [ -s "$AI_LOCAL_TLS_CA_KEY_FILE" ]; then
        ai_local_tls_generate_ca_bundle
        return 0
    fi

    ai_local_tls_log "generating ai-local internal CA at ${AI_LOCAL_TLS_CA_CERT_FILE}"
    openssl req \
        -x509 \
        -newkey rsa:4096 \
        -sha256 \
        -days "${AI_LOCAL_TLS_CA_DAYS:-3650}" \
        -nodes \
        -subj "/CN=ai-local internal CA" \
        -keyout "$AI_LOCAL_TLS_CA_KEY_FILE" \
        -out "$AI_LOCAL_TLS_CA_CERT_FILE" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        >/dev/null 2>&1
    chmod 600 "$AI_LOCAL_TLS_CA_KEY_FILE"
    chmod 644 "$AI_LOCAL_TLS_CA_CERT_FILE"
    ai_local_tls_generate_ca_bundle
}

ai_local_tls_generate_ca_bundle() {
    if [ -s /etc/ssl/certs/ca-certificates.crt ]; then
        cat /etc/ssl/certs/ca-certificates.crt "$AI_LOCAL_TLS_CA_CERT_FILE" > "$AI_LOCAL_TLS_CA_BUNDLE_FILE"
    else
        cp "$AI_LOCAL_TLS_CA_CERT_FILE" "$AI_LOCAL_TLS_CA_BUNDLE_FILE"
    fi
    chmod 644 "$AI_LOCAL_TLS_CA_BUNDLE_FILE"
}

ai_local_tls_generate_service_cert() {
    if [ -s "$AI_LOCAL_TLS_CERT_FILE" ] && [ -s "$AI_LOCAL_TLS_KEY_FILE" ] \
        && openssl x509 -checkend 86400 -noout -in "$AI_LOCAL_TLS_CERT_FILE" >/dev/null 2>&1; then
        return 0
    fi

    ai_local_tls_log "generating TLS certificate for ${AI_LOCAL_TLS_SERVICE_NAME}"
    _csr_file="${AI_LOCAL_TLS_SERVICE_DIR}/tls.csr"
    AI_LOCAL_TLS_EXT_FILE="${AI_LOCAL_TLS_SERVICE_DIR}/tls.ext"
    _serial_file="${AI_LOCAL_TLS_SERVICE_DIR}/tls.serial"
    AI_LOCAL_TLS_DNS_COUNT=0
    AI_LOCAL_TLS_DNS_SEEN=""
    AI_LOCAL_TLS_IP_COUNT=0
    AI_LOCAL_TLS_IP_SEEN=""
    export AI_LOCAL_TLS_EXT_FILE AI_LOCAL_TLS_DNS_COUNT AI_LOCAL_TLS_DNS_SEEN
    export AI_LOCAL_TLS_IP_COUNT AI_LOCAL_TLS_IP_SEEN

    {
        echo "[req]"
        echo "prompt = no"
        echo "distinguished_name = dn"
        echo "req_extensions = v3_req"
        echo "[dn]"
        echo "CN = ${AI_LOCAL_TLS_SERVICE_NAME}"
        echo "[v3_req]"
        echo "basicConstraints = critical,CA:false"
        echo "keyUsage = critical,digitalSignature,keyEncipherment"
        echo "extendedKeyUsage = serverAuth"
        echo "subjectAltName = @alt_names"
        echo "[alt_names]"
    } > "$AI_LOCAL_TLS_EXT_FILE"

    ai_local_tls_add_dns_name "$AI_LOCAL_TLS_SERVICE_NAME"
    ai_local_tls_add_dns_name "$(hostname)"
    ai_local_tls_add_dns_name "localhost"
    _old_ifs="$IFS"
    IFS=","
    for _dns_name in ${AI_LOCAL_TLS_DNS_NAMES:-}; do
        IFS="$_old_ifs"
        ai_local_tls_add_dns_name "$_dns_name"
        IFS=","
    done
    IFS="$_old_ifs"

    ai_local_tls_add_ip_address "127.0.0.1"
    ai_local_tls_add_ip_address "::1"
    IFS=","
    for _ip_address in ${AI_LOCAL_TLS_IP_ADDRESSES:-}; do
        IFS="$_old_ifs"
        ai_local_tls_add_ip_address "$_ip_address"
        IFS=","
    done
    IFS="$_old_ifs"

    openssl genrsa -out "$AI_LOCAL_TLS_KEY_FILE" 3072 >/dev/null 2>&1
    chmod 600 "$AI_LOCAL_TLS_KEY_FILE"
    openssl req -new -key "$AI_LOCAL_TLS_KEY_FILE" -out "$_csr_file" -config "$AI_LOCAL_TLS_EXT_FILE" >/dev/null 2>&1
    openssl x509 \
        -req \
        -in "$_csr_file" \
        -CA "$AI_LOCAL_TLS_CA_CERT_FILE" \
        -CAkey "$AI_LOCAL_TLS_CA_KEY_FILE" \
        -CAserial "$_serial_file" \
        -CAcreateserial \
        -out "$AI_LOCAL_TLS_CERT_FILE" \
        -days "${AI_LOCAL_TLS_CERT_DAYS:-397}" \
        -sha256 \
        -extensions v3_req \
        -extfile "$AI_LOCAL_TLS_EXT_FILE" \
        >/dev/null 2>&1
    chmod 644 "$AI_LOCAL_TLS_CERT_FILE"
    rm -f "$_csr_file"
}

ai_local_tls_install_client_aliases() {
    # Docker CLI expects ca.pem/cert.pem/key.pem under DOCKER_CERT_PATH.
    ln -sf "$AI_LOCAL_TLS_CA_CERT_FILE" "${AI_LOCAL_TLS_SERVICE_DIR}/ca.pem"
    ln -sf "$AI_LOCAL_TLS_CERT_FILE" "${AI_LOCAL_TLS_SERVICE_DIR}/cert.pem"
    ln -sf "$AI_LOCAL_TLS_KEY_FILE" "${AI_LOCAL_TLS_SERVICE_DIR}/key.pem"
}

ai_local_ensure_tls_cert() {
    command -v openssl >/dev/null 2>&1 || ai_local_tls_fatal "openssl is required to serve APIs over TLS"

    AI_LOCAL_TLS_DIR="${AI_LOCAL_TLS_DIR:-/run/ai-local-tls}"
    AI_LOCAL_TLS_SERVICE_NAME="${AI_LOCAL_TLS_SERVICE_NAME:-${SERVICE_NAME:-$(hostname)}}"
    AI_LOCAL_TLS_SERVICE_SAFE="$(printf '%s' "$AI_LOCAL_TLS_SERVICE_NAME" | tr -c 'A-Za-z0-9_.-' '_')"
    AI_LOCAL_TLS_SERVICE_DIR="${AI_LOCAL_TLS_SERVICE_DIR:-${AI_LOCAL_TLS_DIR}/services/${AI_LOCAL_TLS_SERVICE_SAFE}}"
    AI_LOCAL_TLS_CA_CERT_FILE="${AI_LOCAL_TLS_CA_CERT_FILE:-${AI_LOCAL_TLS_DIR}/ca.crt}"
    AI_LOCAL_TLS_CA_KEY_FILE="${AI_LOCAL_TLS_CA_KEY_FILE:-${AI_LOCAL_TLS_DIR}/ca.key}"
    AI_LOCAL_TLS_CA_BUNDLE_FILE="${AI_LOCAL_TLS_CA_BUNDLE_FILE:-${AI_LOCAL_TLS_DIR}/ca-bundle.crt}"
    AI_LOCAL_TLS_CERT_FILE="${AI_LOCAL_TLS_CERT_FILE:-${AI_LOCAL_TLS_SERVICE_DIR}/tls.crt}"
    AI_LOCAL_TLS_KEY_FILE="${AI_LOCAL_TLS_KEY_FILE:-${AI_LOCAL_TLS_SERVICE_DIR}/tls.key}"
    export AI_LOCAL_TLS_DIR AI_LOCAL_TLS_SERVICE_NAME AI_LOCAL_TLS_SERVICE_DIR
    export AI_LOCAL_TLS_CA_CERT_FILE AI_LOCAL_TLS_CA_KEY_FILE AI_LOCAL_TLS_CA_BUNDLE_FILE
    export AI_LOCAL_TLS_CERT_FILE AI_LOCAL_TLS_KEY_FILE

    mkdir -p "$AI_LOCAL_TLS_SERVICE_DIR"
    chmod 700 "$AI_LOCAL_TLS_DIR" "$AI_LOCAL_TLS_SERVICE_DIR" 2>/dev/null || true

    ai_local_tls_lock
    trap ai_local_tls_unlock EXIT INT TERM
    ai_local_tls_generate_ca
    ai_local_tls_generate_service_cert
    ai_local_tls_install_client_aliases
    ai_local_tls_unlock
    trap - EXIT INT TERM

    export SSL_CERT_FILE="${SSL_CERT_FILE:-$AI_LOCAL_TLS_CA_BUNDLE_FILE}"
    export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-$AI_LOCAL_TLS_CA_BUNDLE_FILE}"
    export CURL_CA_BUNDLE="${CURL_CA_BUNDLE:-$AI_LOCAL_TLS_CA_BUNDLE_FILE}"
}

ai_local_ensure_tls_cert

return 0 2>/dev/null || exit 0
