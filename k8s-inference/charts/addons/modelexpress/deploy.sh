#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# ModelExpress Helm Chart Deployment Script
# This script helps deploy ModelExpress using Helm

set -euo pipefail

if [[ "${FS2_EXTERNAL_CAPSULE_ACTIVE:-}" != "1" ]]; then
    printf 'ModelExpress deployment must start through the external capsule shell-entry command\n' >&2
    exit 1
fi
case "${FS2_CAPSULE_SOURCE_ROOT:-}" in /*) ;; *) printf 'capsule read-only source root is absent\n' >&2; exit 1 ;; esac
case "${FS2_CAPSULE_TOOL_DIR:-}" in /*) ;; *) printf 'capsule read-only tool directory is absent\n' >&2; exit 1 ;; esac
export PATH="$FS2_CAPSULE_TOOL_DIR"
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
RELEASE_NAME="modelexpress"
NAMESPACE="modelexpress"
VALUES_FILE=""
DRY_RUN=false
UPGRADE=false
CHART_DIR="${FS2_CAPSULE_SOURCE_ROOT}/charts/addons/modelexpress"
SECURITY_DIR="${FS2_CAPSULE_SOURCE_ROOT}/security"
RELEASE_CLOSURE="${FS2_SAI24_RELEASE_CLOSURE:-}"
TOOLCHAIN="${FS2_IMAGE_GATE_TOOLCHAIN:-}"
GATE_BOOTSTRAP="${FS2_IMAGE_GATE_BOOTSTRAP:-}"
EXTERNAL_TRUST="${FS2_EXTERNAL_CAPSULE_TRUST:-}"
reviewed_helm=()
reviewed_kubectl=()
PULL_AUTH_RECEIPT=""
PULL_ADMISSION_RECEIPT=""
PULL_REFRESH_REGISTRATION=""
PULL_DOCKER_CONFIG=""
PULL_SUBJECT=""
PULL_IDENTITY_FILE=""
PULL_TOKEN_FILE=""

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to show usage
show_usage() {
    cat << EOF
Usage: $0 [OPTIONS]

Required environment:
    FS2_SAI24_RELEASE_CLOSURE
                              Signed release closure whose source and values
                              exactly authorize this invocation
    FS2_IMAGE_GATE_TOOLCHAIN  Source-pinned execution toolchain lock
    FS2_IMAGE_GATE_BOOTSTRAP  Externally installed root-owned capsule bootstrap
    FS2_EXTERNAL_CAPSULE_TRUST
                              External root-owned read-only trust document
    FS2_WORKLOAD_OIDC_TOKEN_FILE
                              Projected short-lived workload identity token
    FS2_WORKLOAD_AUTH_IDENTITY
                              Exact reviewed service-account identity JSON
    FS2_WORKLOAD_AUTH_RUN_ROOT
                              Existing private directory for ephemeral auth files
    FS2_MODELEXPRESS_IMAGE    Exact repository@sha256 pull subject

Options:
    -r, --release-name NAME    Release name (default: modelexpress)
    -n, --namespace NAME       Kubernetes namespace (default: modelexpress)
    -f, --values FILE          Values file to use (e.g., values-production.yaml)
    -d, --dry-run              Perform a dry run
    -u, --upgrade              Upgrade existing release
    -h, --help                 Show this help message

Available values files (in script directory):
    values-production.yaml     Production configuration
    values-development.yaml    Development configuration
    values.yaml               Default configuration
    test-values.yaml          Test configuration

Examples:
    # Deploy with default values
    $0

    # Deploy with production values
    $0 -f values-production.yaml

    # Deploy with custom release name and namespace
    $0 -r my-modelexpress -n my-namespace

    # Perform a dry run
    $0 -d

    # Upgrade existing release
    $0 -u -f values-production.yaml

EOF
}

# Function to find default values files
find_default_values() {
    local script_dir="$CHART_DIR"
    local available_files=()

    # Check for common values files in the script directory
    for file in "values-production.yaml" "values-development.yaml" "values.yaml" "test-values.yaml"; do
        if [[ -f "$script_dir/$file" ]]; then
            available_files+=("$file")
        fi
    done

    if [[ ${#available_files[@]} -gt 0 ]]; then
        print_status "Available values files in modelexpress:"
        for i in "${!available_files[@]}"; do
            echo "  $((i+1)). ${available_files[$i]}"
        done
        echo ""
    fi
}

# Function to check prerequisites
check_prerequisites() {
    print_status "Checking prerequisites..."

    # Check if we can connect to Kubernetes cluster
    if ! "${reviewed_kubectl[@]}" cluster-info &> /dev/null; then
        print_error "Cannot connect to Kubernetes cluster. Please check your kubeconfig."
        exit 1
    fi

    print_success "Prerequisites check passed"
}

# The release helper never creates an unaudited namespace before the actual
# post-render equality gate. Namespace ownership remains an explicit platform
# prerequisite.
require_namespace() {
    if ! "${reviewed_kubectl[@]}" get namespace "$NAMESPACE" &> /dev/null; then
        print_error "Namespace $NAMESPACE must be provisioned by the reviewed platform stage."
        exit 1
    fi
}

acquire_pull_authorization() {
    PULL_TOKEN_FILE="${FS2_WORKLOAD_OIDC_TOKEN_FILE:-}"
    PULL_IDENTITY_FILE="${FS2_WORKLOAD_AUTH_IDENTITY:-}"
    local auth_root="${FS2_WORKLOAD_AUTH_RUN_ROOT:-}"
    PULL_SUBJECT="${FS2_MODELEXPRESS_IMAGE:-}"
    case "$PULL_TOKEN_FILE" in /*) ;; *) print_error "FS2_WORKLOAD_OIDC_TOKEN_FILE must be absolute."; exit 1 ;; esac
    case "$PULL_IDENTITY_FILE" in /*) ;; *) print_error "FS2_WORKLOAD_AUTH_IDENTITY must be absolute."; exit 1 ;; esac
    case "$auth_root" in /*) ;; *) print_error "FS2_WORKLOAD_AUTH_RUN_ROOT must be absolute."; exit 1 ;; esac
    case "$PULL_SUBJECT" in *@sha256:????????????????????????????????????????????????????????????????) ;; *) print_error "FS2_MODELEXPRESS_IMAGE must be digest-bound."; exit 1 ;; esac
    if [[ ! -d "$auth_root" || -L "$auth_root" ]]; then
        print_error "Workload auth root must be an existing private directory."
        exit 1
    fi
    PULL_DOCKER_CONFIG="$auth_root/modelexpress-dockerconfig.json"
    PULL_AUTH_RECEIPT="$auth_root/modelexpress-pull-receipt.json"
    PULL_REFRESH_REGISTRATION="$auth_root/modelexpress-refresh-registration.json"
    PULL_ADMISSION_RECEIPT="$auth_root/modelexpress-secret-admission-receipt.json"
    FS2_IMAGE_GATE_TOOLCHAIN="$TOOLCHAIN" \
    FS2_IMAGE_GATE_TRUST="$SECURITY_DIR/image-attestation-trust.json" \
      "$GATE_BOOTSTRAP" python-entry \
        --external-trust "$EXTERNAL_TRUST" \
        --toolchain "$TOOLCHAIN" \
        --source-root "$SECURITY_DIR/.." \
        --entry security/oidc_attestation_broker.py -- \
        workload-registry-auth \
        --identity "$PULL_IDENTITY_FILE" \
        --token-file "$PULL_TOKEN_FILE" \
        --trust "$SECURITY_DIR/image-attestation-trust.json" \
        --subject "$PULL_SUBJECT" \
        --docker-config-output "$PULL_DOCKER_CONFIG" \
        --receipt-output "$PULL_AUTH_RECEIPT"
}

activate_pull_authorization() {
    "$GATE_BOOTSTRAP" register-workload-registry-refresh \
        --external-trust "$EXTERNAL_TRUST" \
        --toolchain "$TOOLCHAIN" \
        --source-root "$SECURITY_DIR/.." \
        --trust "$SECURITY_DIR/image-attestation-trust.json" \
        --controller-contract "$SECURITY_DIR/workload-registry-refresh-contract.json" \
        --identity "$PULL_IDENTITY_FILE" \
        --token-file "$PULL_TOKEN_FILE" \
        --receipt "$PULL_AUTH_RECEIPT" \
        --subject "$PULL_SUBJECT" \
        --namespace "$NAMESPACE" \
        --secret-name fs2-modelexpress-pull \
        --output "$PULL_REFRESH_REGISTRATION"
    "$GATE_BOOTSTRAP" apply-registry-secret \
        --external-trust "$EXTERNAL_TRUST" \
        --toolchain "$TOOLCHAIN" \
        --source-root "$SECURITY_DIR/.." \
        --trust "$SECURITY_DIR/image-attestation-trust.json" \
        --planning-receipt "$PULL_AUTH_RECEIPT" \
        --refresh-registration "$PULL_REFRESH_REGISTRATION" \
        --broker-fresh-credential-at-secret-admission \
        --identity "$PULL_IDENTITY_FILE" \
        --token-file "$PULL_TOKEN_FILE" \
        --controller-contract "$SECURITY_DIR/workload-registry-refresh-contract.json" \
        --admission-contract "$SECURITY_DIR/workload-registry-secret-admission-contract.json" \
        --minimum-remaining-ttl-seconds 600 \
        --maximum-readiness-age-seconds 60 \
        --admission-receipt-output "$PULL_ADMISSION_RECEIPT" \
        --subject "$PULL_SUBJECT" \
        --namespace "$NAMESPACE" \
        --secret-name fs2-modelexpress-pull
}

configure_reviewed_tools() {
    case "$RELEASE_CLOSURE" in /*) ;; *) print_error "FS2_SAI24_RELEASE_CLOSURE must be absolute."; exit 1 ;; esac
    case "$TOOLCHAIN" in /*) ;; *) print_error "FS2_IMAGE_GATE_TOOLCHAIN must be absolute."; exit 1 ;; esac
    case "$GATE_BOOTSTRAP" in /*) ;; *) print_error "FS2_IMAGE_GATE_BOOTSTRAP must be absolute."; exit 1 ;; esac
    case "$EXTERNAL_TRUST" in /*) ;; *) print_error "FS2_EXTERNAL_CAPSULE_TRUST must be absolute."; exit 1 ;; esac
    reviewed_helm=(
        "$GATE_BOOTSTRAP" exec-tool
        --external-trust "$EXTERNAL_TRUST"
        --toolchain "$TOOLCHAIN"
        --source-root "$SECURITY_DIR/.."
        --tool helm --
    )
    reviewed_kubectl=(
        "$GATE_BOOTSTRAP" exec-tool
        --external-trust "$EXTERNAL_TRUST"
        --toolchain "$TOOLCHAIN"
        --source-root "$SECURITY_DIR/.."
        --tool kubectl --
    )
}

# Function to deploy the chart
deploy_chart() {
    local helm_args=()
    local render_args=()
    local mode=install
    local image_gate=(
        --post-renderer "$GATE_BOOTSTRAP"
        --post-renderer-args=python-entry
        --post-renderer-args=--external-trust
        --post-renderer-args "$EXTERNAL_TRUST"
        --post-renderer-args=--toolchain
        --post-renderer-args "$TOOLCHAIN"
        --post-renderer-args=--source-root
        --post-renderer-args "$SECURITY_DIR/.."
        --post-renderer-args=--entry
        --post-renderer-args security/helm_image_postrenderer.py
        --post-renderer-args=--
        --post-renderer-args=--lock
        --post-renderer-args "$SECURITY_DIR/third-party-images.lock.json"
        --post-renderer-args=--first-party-lock
        --post-renderer-args "$SECURITY_DIR/first-party-images.lock.json"
        --post-renderer-args=--trust
        --post-renderer-args "$SECURITY_DIR/image-attestation-trust.json"
        --post-renderer-args=--toolchain
        --post-renderer-args "$TOOLCHAIN"
        --post-renderer-args=--authorization
        --post-renderer-args "$RELEASE_CLOSURE"
        --post-renderer-args=--surfaces
        --post-renderer-args "$SECURITY_DIR/release-image-surfaces.json"
        --post-renderer-args=--surface-id
        --post-renderer-args charts/addons/modelexpress
        --post-renderer-args=--release-name
        --post-renderer-args "$RELEASE_NAME"
        --post-renderer-args=--namespace
        --post-renderer-args "$NAMESPACE"
        --post-renderer-args=--chart-path
        --post-renderer-args "$CHART_DIR"
        --post-renderer-args=--registry-auth-receipt
        --post-renderer-args "$PULL_AUTH_RECEIPT"
    )

    if [ -n "$VALUES_FILE" ]; then
        helm_args+=("-f" "$VALUES_FILE")
        image_gate+=(--post-renderer-args=--values-file --post-renderer-args "$VALUES_FILE")
    fi

    helm_args+=("--namespace" "$NAMESPACE")

    if [ "$UPGRADE" = true ]; then
        mode=upgrade
    fi
    image_gate+=(--post-renderer-args=--mode --post-renderer-args "$mode")
    render_args=("${helm_args[@]}")
    if [ "$UPGRADE" = true ]; then
        render_args+=("--is-upgrade")
    fi

    # No cluster or refresh-owner mutation occurs until the exact chart,
    # values, release, namespace, mode and post-rendered bytes match the signed
    # closure. The actual install/upgrade repeats the same equality gate from
    # the immutable capsule source and exact Helm binary.
    print_status "Verifying exact retained render before prerequisite mutation..."
    "${reviewed_helm[@]}" template "$RELEASE_NAME" "$CHART_DIR" \
        "${render_args[@]}" "${image_gate[@]}" >/dev/null

    if [ "$DRY_RUN" = true ]; then
        helm_args+=("--dry-run")
        print_status "Performing dry run without applying registry credentials..."
    else
        activate_pull_authorization
    fi

    if [ "$UPGRADE" = true ]; then
        print_status "Upgrading release: $RELEASE_NAME"
        "${reviewed_helm[@]}" upgrade "${helm_args[@]}" "${image_gate[@]}" "$RELEASE_NAME" "$CHART_DIR"
    else
        print_status "Installing release: $RELEASE_NAME"
        "${reviewed_helm[@]}" install "${helm_args[@]}" "${image_gate[@]}" "$RELEASE_NAME" "$CHART_DIR"
    fi

    print_success "Deployment completed successfully"
}

# Function to show deployment status
show_status() {
    print_status "Checking deployment status..."

    echo
    echo "Pods:"
    "${reviewed_kubectl[@]}" get pods -n "$NAMESPACE" -l app.kubernetes.io/name=modelexpress

    echo
    echo "Services:"
    "${reviewed_kubectl[@]}" get svc -n "$NAMESPACE" -l app.kubernetes.io/name=modelexpress

    echo
    echo "PersistentVolumeClaims:"
    "${reviewed_kubectl[@]}" get pvc -n "$NAMESPACE" -l app.kubernetes.io/name=modelexpress
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -r|--release-name)
            RELEASE_NAME="$2"
            shift 2
            ;;
        -n|--namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        -f|--values)
            VALUES_FILE="$2"
            shift 2
            ;;
        -d|--dry-run)
            DRY_RUN=true
            shift
            ;;
        -u|--upgrade)
            UPGRADE=true
            shift
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            show_usage
            exit 1
            ;;
    esac
done

# Main execution
main() {
    print_status "Starting ModelExpress Helm deployment..."
    print_status "Release: $RELEASE_NAME"
    print_status "Namespace: $NAMESPACE"
    if [[ -n "$VALUES_FILE" ]]; then
        print_status "Values file: $VALUES_FILE"
    else
        print_status "Values file: Using default values"
    fi

    configure_reviewed_tools
    find_default_values
    check_prerequisites
    require_namespace
    acquire_pull_authorization
    deploy_chart

    if [ "$DRY_RUN" = false ]; then
        show_status
        print_success "Deployment completed!"
        print_status "Use the reviewed kubectl toolchain for any port-forward operation."
    fi
}

# Run main function
main
