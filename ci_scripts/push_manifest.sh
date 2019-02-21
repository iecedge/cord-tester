export IMAGE_TAG=$(cat VERSION)
export DOCKER_CLI_EXPERIMENTAL=enabled

docker manifest create --amend cachengo/xos-api-tester:$IMAGE_TAG cachengo/xos-api-tester-x86_64:$IMAGE_TAG cachengo/xos-api-tester-aarch64:$IMAGE_TAG
docker manifest push cachengo/xos-api-tester:$IMAGE_TAG
