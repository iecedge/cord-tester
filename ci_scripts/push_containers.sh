export IMAGE_TAG=$(cat VERSION)
export AARCH=`uname -m`
cd src/test/cord-api
docker build -f Dockerfile.k8s-api-tester -t cachengo/xos-api-tester-$AARCH:$IMAGE_TAG .
docker push cachengo/xos-api-tester-$AARCH:$IMAGE_TAG