#!/usr/bin/python
# -*- coding:utf-8 -*-

import os, time, json, tempfile, subprocess, signal, re
import logging
from optparse import OptionParser, OptionGroup

__version__ = "0.1"
OPTIONS = None
WHERE_IS_GENERATOR = "../base_image_generator"
CMD_TRANSFORM_DOCKERFILE = "python ../base_image_generator/base_image_generator.py -f %s -o %s --build --test" # path to the Dockerfile and output folder
CMD_BUILD_IMAGE = "docker build -t %s -f %s ." # tag name and Dockerfile path
CMD_RUN_APPLICATION = "docker run --rm -p 4000:4000 %s-pobs:%d" # give the project name and the index of the dockerfile
CLEAN_CONTAINERS_SINCE = "CONTAINER_NAME" # give a container name since which the created containers will be removed
os.environ["TMPDIR"] = "/tmp" # change it if you need to use another path to create temporary files

def parse_options():
    usage = r'usage: python3 %prog [options] -f /path/to/json-data-set.json'
    parser = OptionParser(usage = usage, version = __version__)

    parser.add_option('-d', '--dataset',
        action = 'store',
        type = 'string',
        dest = 'dataset',
        help = 'The path to the projects dataset (json format)'
    )

    options, args = parser.parse_args()
    if options.dataset == None:
        parser.print_help()
        parser.error("The path to the json dataset should be given.")
    elif options.dataset != None and not os.path.isfile(options.dataset):
        parser.print_help()
        parser.error("%s should be a json file."%options.dataset)
    
    return options

def run_command(command, workdir, timeout=600):
    with tempfile.NamedTemporaryFile(mode="w+b") as stdout_f, tempfile.NamedTemporaryFile(mode="w+b") as stderr_f:
        p = subprocess.Popen(command, stdout=stdout_f.fileno(), stderr=stderr_f.fileno(), close_fds=True, shell=True, cwd=workdir, preexec_fn=os.setsid)
        try:
            exit_code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired as err:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            exit_code = -1
        stdout_f.flush()
        stderr_f.flush()
        stdout_f.seek(0, os.SEEK_SET)
        stderr_f.seek(0, os.SEEK_SET)
        stdoutdata = stdout_f.read()
        stderrdata = stderr_f.read()

        return (stdoutdata.decode("utf-8"), stderrdata.decode("utf-8"), exit_code)

def run_original_image(image_name):
    with tempfile.NamedTemporaryFile(mode="w+b") as stdout_f, tempfile.NamedTemporaryFile(mode="w+b") as stderr_f:
        continuously_running = False
        java_process_detected = False
        p = subprocess.Popen("docker run --rm --name run_original %s"%image_name, stdout=stdout_f.fileno(), stderr=stderr_f.fileno(), close_fds=True, shell=True)
        try:
            exit_code = p.wait(timeout=60)
        except subprocess.TimeoutExpired as err:
            exit_code = 0 # if the container runs for 60 seconds, it is considered as a successful run
            continuously_running = True

            # check if there is a java process running in the container
            try:
                top_output = subprocess.check_output("docker top run_original -C java", shell=True).decode("utf-8")
                if "java" in top_output: java_process_detected = True
            except subprocess.TimeoutExpired as err:
                logging.info(err.output)

            # manually stop the running container
            os.system("docker stop run_original")

        stdout_f.flush()
        stderr_f.flush()
        stdout_f.seek(0, os.SEEK_SET)
        stderr_f.seek(0, os.SEEK_SET)
        stdoutdata = stdout_f.read()
        stderrdata = stderr_f.read()

        return (stdoutdata.decode("utf-8"), stderrdata.decode("utf-8"), exit_code, continuously_running, java_process_detected)

def test_application(project_name, image_index):
    with tempfile.NamedTemporaryFile(mode="w+b") as stdout_f, tempfile.NamedTemporaryFile(mode="w+b") as stderr_f:
        continuously_running = False
        p = subprocess.Popen(CMD_RUN_APPLICATION%(project_name, image_index), stdout=stdout_f.fileno(), stderr=stderr_f.fileno(), close_fds=True, shell=True)
        try:
            exit_code = p.wait(timeout=60)
        except subprocess.TimeoutExpired as err:
            exit_code = 0 # if the container runs for 60 seconds, it is considered as a successful run
            continuously_running = True
        stdout_f.flush()
        stderr_f.flush()
        stdout_f.seek(0, os.SEEK_SET)
        stderr_f.seek(0, os.SEEK_SET)
        stdoutdata = stdout_f.read()
        stderrdata = stderr_f.read()

        glowroot_attached = False
        tripleagent_attached = False
        stdout = stdoutdata.decode("utf-8")
        if "INFO  org.glowroot - Glowroot version: 0.13.5" in stdout:
            glowroot_attached = True
        if "INFO TripleAgent PerturbationAgent is successfully attached!" in stdout:
            tripleagent_attached = True

        return (glowroot_attached, tripleagent_attached, stdoutdata.decode("utf-8"), stderrdata.decode("utf-8"), exit_code, continuously_running)

def dump_logs(stdoutdata, stderrdata, filepath, fileprefix):
    os.makedirs(filepath, exist_ok=True)
    with open(os.path.join(filepath, "%s-stdout.log"%fileprefix), 'wt') as stdoutfile, \
            open(os.path.join(filepath, "%s-stderr.log"%fileprefix), 'wt') as stderrfile:
        stdoutfile.writelines(stdoutdata)
        stderrfile.writelines(stderrdata)

def analyze_loc(project_path):
    result = dict()

    # the 4 groups of number: files, blank, comment, code
    pattern_java = re.compile(r'Java\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)')
    pattern_sum = re.compile(r'SUM:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)')

    cloc_output = subprocess.check_output("cloc %s"%(project_path), shell=True).decode("utf-8")
    match_java = pattern_java.search(cloc_output)
    match_sum = pattern_sum.search(cloc_output)
    if match_java != None:
        result["java"] = {"files": match_java.group(1), "code": match_java.group(4)}
    if match_sum != None:
        result["sum"] = {"files": match_sum.group(1), "code": match_sum.group(4)}

    return result

def clean_up_project(project_name):
    os.system("docker stop $(docker ps -q)") # stop all containers first
    os.system("docker rm $(docker ps -a -q --filter since=%s)"%CLEAN_CONTAINERS_SINCE)
    os.system("docker image prune -f")
    os.system("docker image rm -f $(docker images -q --filter reference='%s-pobs:*')"%project_name)
    os.system("docker image rm -f $(docker images -q --filter reference='%s:*')"%project_name)

def evaluate_project(project):
    if project["number_of_dockerfiles"] > 0:
        # clone the repo
        with tempfile.TemporaryDirectory() as tmpdir:
            tmprepo = os.path.join(tmpdir, project["name"])
            goodrepo = os.system("cd %s && git clone %s"%(tmpdir, project["clone_url"])) == 0

            if goodrepo:
                goodcheckout = os.system("cd %s && git checkout %s"%(tmprepo, project["commit_sha"])) == 0
                if goodcheckout:
                    project["is_able_to_clone"] = True
                    project["is_able_to_build"] = list()
                    project["is_able_to_run"] = list()
                    project["loc_info"] = analyze_loc(tmprepo)
                else:
                    project["is_able_to_clone"] = False
                    logging.error("failed to checkout the specific commit of repo %s, commit %s"%(project["clone_url"], project["commit_sha"]))
                    return project
            else:
                project["is_able_to_clone"] = False
                logging.error("failed to clone repo %s"%project["clone_url"])
                return project

            # build the project?
            if "build_tools" in project:
                logging.info("Try to build the project with some default commands")
                if "Maven" in project["build_tools"]:
                    stdout, stderr, exitcode = run_command("mvn package -DskipTests=true", tmprepo)
                    if exitcode == 0: project["is_able_to_build"].append("Maven")
                if "Gradle" in project["build_tools"]:
                    stdout, stderr, exitcode = run_command("gradle build -x test", tmprepo)
                    if exitcode == 0: project["is_able_to_build"].append("Gradle")
                if "Ant" in project["build_tools"]:
                    stdout, stderr, exitcode = run_command("ant compile jar", tmprepo)
                    if exitcode == 0: project["is_able_to_build"].append("Ant")
                if len(project["is_able_to_build"]) > 0:
                    logging.info("Successfully build the project using: %s"%project["is_able_to_build"])

            fileindex = 0
            project_name = project["name"].lower() # docker build: repository name must be lowercase
            project_full_name = project["full_name"].lower().replace("/", "_")
            for dockerfile in project["info_from_dockerfiles"]:
                # build the base image
                filepath = os.path.join(tmprepo, dockerfile["path"])
                dirname = os.path.dirname(filepath)
                filename = os.path.basename(filepath)

                # sanity check: try to build the original docker image
                logging.info("Check whether the dockerfile is buildable: %s"%dockerfile["path"])
                stdout, stderr, exitcode = run_command(CMD_BUILD_IMAGE%(project_name, filename), dirname)
                if exitcode != 0:
                    dockerfile["sanity_check"] = "failed"
                    dump_logs(stdout, stderr, "./logs/sanity_check/", "%s_%d_sanity_check"%(project_full_name, fileindex))
                    logging.info("The original dockerfile can not be built: %s"%dockerfile["path"])
                else:
                    dockerfile["sanity_check"] = "successful"

                    # check if the original docker image can be run for 1 min
                    logging.info("Begin to check if the original docker image can be run for 1 min, with a java process detected")
                    stdout, stderr, exitcode, continuously_running, java_process_detected = run_original_image(project_name)
                    logging.info("ori_application_run, exitcode: %d, continuously: %s, java: %s"%(exitcode, continuously_running, java_process_detected))
                    dockerfile["ori_application_run_exitcode"] = exitcode
                    dockerfile["ori_application_run_continuously"] = continuously_running
                    dockerfile["ori_application_run_java"] = java_process_detected
                    if continuously_running and java_process_detected:
                        # POBS base image generation test
                        logging.info("Begin to transform the dockerfile and build POBS base image: %s"%dockerfile["path"])
                        stdout, stderr, exitcode = run_command(CMD_TRANSFORM_DOCKERFILE%(filepath, dirname), WHERE_IS_GENERATOR)
                        if exitcode != 0:
                            dump_logs(stdout, stderr, "./logs/base/", "%s_%d_base"%(project_full_name, fileindex))
                            dockerfile["pobs_base_generation"] = "failed"
                            logging.info("Failed to build POBS base image, exitcode: %d"%exitcode)
                        else:
                            dockerfile["pobs_base_generation"] = "successful"

                            # build the PBOS application image
                            logging.info("Begin to build the application image using: %s-pobs-application"%(dockerfile["path"]))
                            stdout, stderr, exitcode = run_command(CMD_BUILD_IMAGE%(project_name + "-pobs:%d"%fileindex, "Dockerfile-pobs-application"), dirname)
                            if exitcode != 0:
                                dump_logs(stdout, stderr, "./logs/app-build/", "%s_%d_appbuild"%(project_full_name, fileindex))
                                dockerfile["pobs_application_build"] = "failed"
                                logging.error("Failed to build the application image using %s-pobs-application, project %s"%(dockerfile["path"], project_name))
                            else:
                                dockerfile["pobs_application_build"] = "successful"

                                # run the application and test Glowroot, TripleAgent
                                glowroot_attached, tripleagent_attached, stdout, stderr, exitcode, continuously_running = test_application(project_name, fileindex)
                                dockerfile["glowroot_attached"] = glowroot_attached
                                dockerfile["tripleagent_attached"] = tripleagent_attached
                                dockerfile["pobs_application_run_exitcode"] = exitcode
                                dockerfile["pobs_application_run_continuously"] = continuously_running

                                if glowroot_attached and tripleagent_attached and continuously_running:
                                    dockerfile["pobs_application_run"] = "successful"
                                    project["is_able_to_run"].append(fileindex)
                                else:
                                    dockerfile["pobs_application_run"] = "failed"
                                    dump_logs(stdout, stderr, "./logs/app-run/", "%s_%d_apprun"%(project_full_name, fileindex))
                fileindex = fileindex + 1
                os.system("docker stop $(docker ps -q)") # stop all containers first
                time.sleep(1)
        # clean up: delete the built images
        clean_up_project(project_name)
    return project

def dump_analysis(projects):
    with open("analysis_log.json", "wt") as output:
        json.dump(projects, output, indent = 4)

def main():
    global OPTIONS
    OPTIONS = parse_options()

    with open(OPTIONS.dataset, 'rt') as file:
        projects = json.load(file)
        for project in projects:
            if project["number_of_dockerfiles"] > 0 and not "is_able_to_clone" in project:
                project = evaluate_project(project)
                dump_analysis(projects)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()