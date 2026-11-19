# Deploying SetuHaul to an existing Amazon EKS cluster

The API uses a local SQLite database. The Kubernetes deployment therefore intentionally
uses one replica and an EBS-backed `gp3` persistent volume; it is a single-instance
pilot deployment, not a horizontally scalable production architecture. The EBS CSI
driver and a `gp3` StorageClass must exist in the target cluster. The cluster must have
an x86_64 node able to schedule the pod.

The application Service is `ClusterIP` only. It has no authentication or TLS, so it is
not exposed publicly by these manifests. Reach it through a temporary port-forward:

```bash
kubectl port-forward -n setuhaul svc/setuhaul-service 8080:80
curl http://127.0.0.1:8080/health
```

Open `http://127.0.0.1:8080/deployment` in a browser to view app health and the EKS target profile.

## Requirements

- An existing EKS cluster with worker nodes and the EBS CSI driver installed.
- AWS CLI v2 credentials with access to ECR and the cluster's EKS authentication role.
- Docker, `kubectl`, and `git`.
- ECR push permissions and Kubernetes permissions to create the `setuhaul` namespace,
  PVC, Deployment, and Service.

## Deploy

From the repository root, set the AWS region and existing cluster name:

```bash
export AWS_REGION=ap-south-1
export EKS_CLUSTER_NAME=my-existing-cluster
./scripts/deploy-eks.sh
```

The script creates the ECR repository if missing, builds and pushes an image, applies
the manifests, waits for the PVC and rollout, and prints the resulting resources. It
does not create or modify the EKS cluster or worker nodes. ECR image storage and EBS
volume charges apply; the cluster itself is billed separately by AWS.

The `/deployment` dashboard reports the running app's health and runtime metadata and
summarizes the repository's EKS target configuration. `/deployment/status` does not
call AWS or the Kubernetes API, so ECR, PVC binding, and rollout conditions must still
be verified with the authenticated AWS CLI and `kubectl` commands above.

To run the end-to-end checks in the same container image before deployment:

```bash
docker build --platform linux/amd64 -t setuhaul-service:e2e .
docker run --rm setuhaul-service:e2e python -m unittest discover -s tests -v
```

## Operational limits

- Do not increase `replicas` while SQLite is the application store.
- The PVC contains runtime appointments, holds, message history, and audit records.
  Back it up before replacing or deleting the claim.
- The sample API has no caller authentication, inbound webhook signature validation,
  outbound messaging adapter, or public TLS. Keep it private until those are added.
- For a multi-replica deployment, move transactional state to a shared database and add
  authenticated ingress before enabling autoscaling or public access.
