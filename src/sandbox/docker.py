import docker
from docker.errors import ImageNotFound, BuildError
import os
import sys
import hashlib
from buildlog import log, error, debug, warn, verbatim


# With the original exec_run there's no way to get to the exit code of a streamed response.
# Monkey patching for now, pull request will follow.
def monkey_patched_exec_run(self, cmd, stdout=True, stderr=True, stdin=False, tty=False,
                            privileged=False, user='', detach=False,
                            socket=False, environment=None, workdir=None):
    resp = self.client.api.exec_create(
        self.id, cmd, stdout=stdout, stderr=stderr, stdin=stdin, tty=tty,
        privileged=privileged, user=user, environment=environment,
        workdir=workdir,
    )
    exec_output = self.client.api.exec_start(
        resp['Id'], detach=detach, tty=tty, stream=True, socket=socket,
        demux=False
    )
    return resp['Id'], exec_output


def get_exit_code(client, exec_id):
    return client.api.exec_inspect(exec_id)['ExitCode']


class DockerContainer:
    def __init__(self, docker_file, git_dir):
        self.client = docker.from_env()
        if not os.path.exists(git_dir):
            raise Exception("Git directory does not exist! " + git_dir)
        if not os.path.isfile(docker_file):
            raise Exception("File does not exist or is not a file! " + docker_file)
        self.dockerfile = os.path.basename(docker_file)
        self.dockerfile_dir = os.path.dirname(docker_file)
        self.dockerfile_hash = get_hash_value_for_file(docker_file)
        self.git_dir = git_dir
        self.container = None

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.clean_containers()

    def jarvis_directory(self) -> str:
        return "/jarvis/"

    def rebuild_image_if_changed(self):
        try:
            self.client.images.get(self.image_name())
            log("Found previously built container image " + self.image_name())
        except ImageNotFound:
            self.build_new_container()

    def build_new_container(self):
        try:
            log("No image found. Rebuilding container. This may take a while.")
            self.client.images.build(path=self.dockerfile_dir, dockerfile=self.dockerfile, tag=self.image_name())
            log("Container built: " + self.image_name())
        except BuildError as e:
            error("Container build failed. Check the following output for errors:")
            for message in e.build_log:
                error(message)
            sys.exit(1)

    def run_command(self, command):
        if not self.container:
            self.container = self.client.containers.run(self.image_name(), "sleep 1d",
                                                        volumes={self.git_dir: {"bind": self.jarvis_directory(),
                                                                                'mode': 'rw'}},
                                                        working_dir=self.jarvis_directory(),
                                                        detach=True)
        self.container.__class__.exec_run = monkey_patched_exec_run
        exec_id, output = self.container.exec_run(command, environment={"CI": True, "JARVIS_CI": True})
        try:
            for line in output:
                verbatim(line.strip().decode("utf-8"))
        except UnicodeError as e:
            # This issue usually happens when you're running something like Perl in the container.
            # Perl ignores the encoding setting of stdout and spits out incompatible encodings...
            warn("Failed to decode the output as unicode. Falling back to printing undecoded byte strings. Sorry!")
            for line in output:
                verbatim(line.strip())
        exit_code = get_exit_code(self.client, exec_id)
        if exit_code != 0:
            error("Command finished with exit code " + str(exit_code))
            sys.exit(exit_code)
        else:
            log("Command finished with exit code " + str(exit_code), colour="blue")

    def clean_containers(self):
        self.container.stop(timeout=10)
        self.client.containers.prune()

    def image_name(self) -> str:
        return "jarvis-image-" + self.dockerfile_hash


def get_hash_value_for_file(file) -> str:
    digest = hashlib.sha1()
    with open(file, 'rb') as some_file:
        buf = some_file.read()
        digest.update(buf)
    return digest.hexdigest()
