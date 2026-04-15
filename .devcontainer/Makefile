.PHONY: start build build-nocache buildx integration test clean-start test-in-container

# Make file for building the image, cleaning, pushing and running locally and remote.

# If the image does not exits, it will build it first, then run it.
# If the image exists but is not started it will create a new container.
# If the container is stopped then it will start it again.
# if the container is running it will attach a new shell to it.
start:
	@bash -c ' \
	source ./makefile.sh; \
	start  \
	echo "Done"; '

# Target for building the image
build-nocache: 
	@bash -c ' \
	source ./makefile.sh; \
	buildNoCache; \
	echo "Done"; '

# Target for building the image
build: 
	@bash -c ' \
	source ./makefile.sh; \
	build; \
	echo "Done"; '

buildx:
	@bash -c ' \
	source ./makefile.sh; \
	buildx; \
	echo "Done"; '
	
integration:
	@bash -c ' \
	source ./makefile.sh; \
	integration  \
	echo "Done"; '

# Run unit tests (bats) — no Docker or K8s required
test:
	@echo "Running unit tests..."
	@bats test/unit/

# Kill and remove the framework container, then start fresh
clean-start:
	@bash -c ' \
	source ./makefile.sh; \
	cleanStart; \
	echo "Done"; '

# Run unit tests inside the Docker container (runtime validation)
# Installs bats if not present (as root), then runs the test suite
test-in-container:
	@bash -c ' \
	source ./makefile.sh; \
	echo "Running unit tests inside container..."; \
	RUN_TESTS="cd /workspaces/$$RepositoryName && bats .devcontainer/test/unit/"; \
	status=$$(docker inspect -f "{{.State.Status}}" "$$IMAGENAME" 2>/dev/null); \
	if [ "$$status" = "running" ]; then \
		docker exec --user root -t "$$IMAGENAME" bash -c "command -v bats >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq bats; }" && \
		docker exec -t "$$IMAGENAME" bash -c "$$RUN_TESTS"; \
	else \
		echo "Container not running. Use make start or make clean-start first."; \
		exit 1; \
	fi; \
	echo "Done"; '