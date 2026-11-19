#!/usr/bin/env bash
set -euo pipefail

: "${AWS_REGION:?Set AWS_REGION to the target AWS region}"
: "${EKS_CLUSTER_NAME:?Set EKS_CLUSTER_NAME to an existing EKS cluster}"

IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPOSITORY="setuhaul-service"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${REGISTRY}/${REPOSITORY}:${IMAGE_TAG}"

if ! aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$REPOSITORY" >/dev/null 2>&1; then
    aws ecr create-repository \
        --region "$AWS_REGION" \
        --repository-name "$REPOSITORY" \
        --image-scanning-configuration scanOnPush=true \
        --encryption-configuration encryptionType=AES256 >/dev/null
fi

aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "$REGISTRY"
docker build --platform linux/amd64 --tag "$IMAGE" .
docker push "$IMAGE"

aws eks update-kubeconfig --region "$AWS_REGION" --name "$EKS_CLUSTER_NAME"
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/storage.yaml
kubectl wait --for=jsonpath='{.status.phase}=Bound' pvc/setuhaul-data \
    --namespace setuhaul --timeout=5m
kubectl apply -f deploy/k8s/service.yaml
kubectl apply -f deploy/k8s/deployment.yaml
kubectl set image deployment/setuhaul-service "setuhaul-service=${IMAGE}" --namespace setuhaul
kubectl rollout status deployment/setuhaul-service --namespace setuhaul --timeout=10m
kubectl get pods,svc,pvc --namespace setuhaul
