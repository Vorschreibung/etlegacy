#!/usr/bin/env bash
# Runs commands in, or publishes, the ET: Legacy build images.
#
# Images are tagged with the git blob hash of their Dockerfile, so a tag always
# matches the Dockerfile of the checked out commit. Images missing from the
# registry are built locally, which lets pull requests test Dockerfile changes
# without publishing anything.
#
# Usage: misc/ci/docker.sh run <image> <command...>
#        misc/ci/docker.sh publish <image>
#
# <image> is one of lnx-build, lnx-aarch64-build or android-build.
set -euo pipefail

action=$1
name=$2
shift 2

case $name in
	lnx-build) dockerfile=misc/docker/build.Dockerfile ;;
	*) dockerfile=misc/docker/${name%-build}.Dockerfile ;;
esac
image="etlegacy/$name:$(git rev-parse --short=12 "HEAD:$dockerfile")"

case $action in
	run)
		# Prefer a local or published image and only build it as a last resort.
		docker image inspect "$image" >/dev/null 2>&1 ||
			docker pull --quiet "$image" ||
			docker build misc/docker --file "$dockerfile" --tag "$image"

		# Match host ownership and paths so artifacts and warning logs remain
		# usable, and give the container user a writable home without mounting
		# the host one.
		exec docker run --rm \
			--user "$(id -u):$(id -g)" --env HOME=/tmp \
			--env CI --env CI_ETL_DESCRIBE --env CI_ETL_TAG \
			--volume "$PWD:$PWD" --workdir "$PWD" \
			"$image" "$@"
		;;
	publish)
		if docker manifest inspect "$image" >/dev/null 2>&1; then
			echo "$image is already published."
			exit 0
		fi

		docker build misc/docker --file "$dockerfile" --tag "$image" --tag "etlegacy/$name:latest"
		docker push "$image"
		# CI itself only uses the content tags (android excepted), latest is a
		# convenience for everyone else.
		docker push "etlegacy/$name:latest"
		;;
	*)
		echo "Unknown action: $action" >&2
		exit 1
		;;
esac
