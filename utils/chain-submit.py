#!/bin/python3

import argparse
from glob import glob
import os
import re
import sys
from dataclasses import dataclass, field
import yaml
from typing import Dict, Set, List
import json
from json_submit import get_sub_cmd, check_exe
import subprocess
from tempfile import NamedTemporaryFile
import numpy as np
import logging
from copy import deepcopy, copy
from get_jobs import create_connection, create_database, read_database, save_database, get_finished
import pandas as pd
import pprint

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TEMPLATE_SCRIPT = "../scripts/job_script.sh"

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

colormap = {
    0: bcolors.OKGREEN,
    1: bcolors.WARNING,
    2: bcolors.OKBLUE
}

@dataclass
class StepStatus:
    files: Set[int] = field(default_factory=set)
    temp: Set[int] = field(default_factory=set)
    to_process: List[int] = field(default_factory=list)

@dataclass
class Step:
    name: str = ""
    odir: str = ""
    subdir: str = ""
    fcl: str = ""
    local_source: str = ""
    dune_version: str = ""
    dune_qual: str = ""
    idir: str = ""
    ifile: str = ""
    ofile: str = ""
    nevents: int = 50
    outputs: List[int] = field(default_factory=list)
    inputs: List[int] = field(default_factory=list)
    job_config: Dict = field(default_factory=dict)
    status: StepStatus = field(default_factory=StepStatus)

    def fill(self, config:Dict) -> None:
        fields = self.__dict__
        for key, val in config.items():
            if key not in fields or key == 'status':
                logger.critical(f"Key {key} is not a valid step field!")
                sys.exit()
            if isinstance(fields[key], dict):
                d = getattr(self, key)
                d.update(val)
            else:
                setattr(self, key, val)

    def validate(self) -> None:
        mandatory = ['name', 'fcl', 'odir', 'dune_version', 'dune_qual', 'job_config']

        for key in mandatory:
            if getattr(self, key) == "":
                logger.critical(f"Key {key} should exist for each step!")
                sys.exit()
        if self.subdir != "":
            self.odir = os.path.join(self.odir, self.subdir)

    def extend_relative(self, yaml_path:str) -> None:
        fields = ['fcl']
        for key in fields:
            val = getattr(self, key)
            if val[0] == '.':
                abs_path = os.path.realpath(os.path.join(yaml_path, val))
                setattr(self, key, abs_path)

class Process:
    def __init__(self, path_file:str, dry=False, new=False):
        self.dry = dry
        self.new = new
        self._parse_config(path_file)
        self._open_db()
        self._check_path()
        self._clean_finished()
        if self.new:
            self._build_path()
        self._build_filelists()
        self._clear_temp()
        self._compute_process()

    def _get_step_by_name(self, name):
        for local_path in self.path:
            for step in local_path:
                if step.name == name:
                    return step
        return None

    def _clean_finished(self):
        finished, new_db = get_finished(self.conn)
        for i, row in finished.iterrows():
            step = self._get_step_by_name(row['step'])
            self._clear_temp_single(step, row['step_id'])
        logger.info(f"Cleared {len(finished)} finished jobs")
        self.db = new_db
        save_database(self.conn, self.db)

    def _open_db(self):
        self.conn = create_connection(self.db_file)
        create_database(self.conn)
        self.db = read_database(self.conn)

    def _chain_io(self):
        for i in range(1, len(self.path)):
            local_path = self.path[i]
            if self.path[i - 1][0].ofile == "":
                logger.critical("All steps except the last of the path must have an output file to ensure the correct input/output chaining, for now.")
                sys.exit()

            for step in local_path:
                if step.ifile == "":
                    step.ifile = self.path[i - 1][0].ofile
                if step.idir == "":
                    step.idir = self.path[i - 1][0].odir

    def _check_path(self) -> None:
        for local_path in self.path:
            for step in local_path:
                if os.path.exists(step.odir) == self.new: #Equiv to XOR
                    logger.critical(f"{step.odir} does {'already' if self.new else 'not'} exist!")
                    sys.exit(1)
    def _build_path(self) -> None:
        for local_path in self.path:
            for step in local_path:
                os.makedirs(step.odir)
        logger.info("Correctly built the new tree structure!")

    def _parse_config(self, fname: str) -> None:
        with open(fname) as f:
            data = yaml.load(f, Loader=yaml.loader.SafeLoader)

        if 'global' not in data or not isinstance(data['global'], dict):
            logger.critical("Missing 'global' section in config!")
            sys.exit()

        glob_conf = data['global']

        for field in ['nfiles']:
            if field not in glob_conf:
                logger.critical(f"Missing field '{field}' in 'global' section of config!")
                sys.exit()

        # if not os.path.exists(glob_conf['odir']):
        #     logger.critical(f"Odir path {glob_conf['odir']} does not exist!")
        #     sys.exit()
        script_file, ext = os.path.splitext(fname)
        self.db_file = script_file + '.sqlite'
        
        self.N = int(glob_conf['nfiles'])
        glob_conf.pop('nfiles')

        template_step = Step()
        template_step.fill(glob_conf)

        if 'path' not in data or not isinstance(data['path'], list):
            logger.critical("Missing 'path' section in config!")
            sys.exit()
        
        path = []
        for i, s in enumerate(data['path']):
            slist = s
            if not isinstance(slist, list):
                slist = [s]
            else:
                if i != len(data['path']) - 1:
                    logging.critical("Multiple concurrent steps are only allowed at the end of the path!")
                    sys.exit()

            local_path = []

            for step_elt in slist:
                step = deepcopy(template_step)
                step.fill(step_elt)
                step.validate()
                step.extend_relative(os.path.dirname(fname))
                local_path.append(step)
            path.append(local_path)
        self.path = path

        self._chain_io()
        

    def _build_filelists(self) -> None:
        for local_path in self.path:
            for step in local_path:
                files = glob(f"{step.odir}/*.root")
                files = map(os.path.basename, files)
                numbers_files = []
                for f in files:
                    matches = re.search(r"_(\d+).root", f)
                    if matches:
                        numbers_files.append(int(matches.group(1)))
                numbers_files = [nb for nb in numbers_files if nb < self.N]
                step.status.files = set(numbers_files)

                temp = glob(f"{step.odir}/*.temp")
                temp = map(os.path.basename, temp)
                numbers_temp = [int(re.search(r"(\d+)", f).group(1)) for f in temp]
                numbers_temp = [nb for nb in numbers_temp if nb < self.N]
                step.status.temp = set(numbers_temp)

    def _compute_process(self) -> None:
        flat_path = [s for local_path in self.path for s in local_path]
        mat = np.ones((self.N, len(flat_path)), dtype=int)
        for j, step in enumerate(flat_path):
            for i in step.status.files:
                mat[i, j] = 0
            for i in step.status.temp:
                mat[i, j] = 2
        self.state = mat

        for i in range(self.N):
            next_steps = self._get_next_steps(i)
            if next_steps is not None:
                for next_step in next_steps:
                    next_step.status.to_process.append(i)

    def _send_jobs(self, step:Step) -> None:
        map_file = self._create_map(step)
        map_file_url = f"dropbox://{map_file}"

        setup_file = self._create_setup(step, os.path.basename(map_file))
        setup_file_url = f"dropbox://{setup_file}"

        input_files = [map_file_url, setup_file_url]

        if not os.path.exists(step.fcl):
            logger.warning(f"{step.fcl} does not exist locally, it is expected to be in FHICLPATH then!")
        else:
            input_files.append(f'dropbox://{step.fcl}')
        

        config = {}
        for key, val in step.job_config.items():
            if len(key) > 1:
                new_key = f"--{key}"
            else:
                new_key = f"-{key}"
            config[new_key] = str(val)

        if "-f" in config:
            logger.critical("You should use the inputs field rather than directly f under job_config")
            sys.exit()

        if step.local_source != "":
            if ".tar.gz" not in os.path.basename(step.local_source):
                logger.critical("A .tar.gz has to be provided as local_source")
                sys.exit()
            if not os.path.exists(step.local_source):
                logger.critical(f"Source archive {step.local_source} does not exist!")
                sys.exit()
            logger.info(f"Using local source {step.local_source}")
            config["--tar_file_name"] = f"dropbox://{step.local_source}"


        for ifile in step.inputs:
            if not os.path.exists(ifile):
                logger.critical(f"Input file {ifile} does not exist!")
                sys.exit()
            input_files.append(f"dropbox://{ifile}")

        config["-f"] = input_files
        config["-N"] = str(len(step.status.to_process))

        bash_script = f"file://{os.path.realpath(os.path.join(os.path.dirname(__file__), TEMPLATE_SCRIPT))}"
        config[bash_script] = []

        jobid = self._submit_job(config)
        jid, server = jobid.split('@')
        clusterid = jid.split('.')[0]

        if not self.dry:
            new_rows = []
            for i, jid in enumerate(step.status.to_process):
                cur_jobid = f"{clusterid}.{i}@{server}"
                self._create_temp(step, jid, cur_jobid)
                db_row = {
                    'jobid': cur_jobid,
                    'status': 'I',
                    'step': step.name,
                    'step_id': jid
                }
                new_rows.append(db_row)
            new_rows = pd.DataFrame(new_rows)
            self.db = pd.concat([self.db, new_rows], ignore_index=True)
        save_database(self.conn, self.db)

    def _submit_job(self, config:Dict) -> int:
        cmd = get_sub_cmd(config, escape_val=True)
        logger.info(f"Command to execute: {cmd}")

        jobid = "999999.0@test.fnal.gov"
        if not self.dry:
            logger.info("Calling jobsub with this command")
            ret = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            print(ret.stdout)
            ret.check_returncode()
            jobid = self._extract_jobid(ret.stdout)
        return jobid


    def submit(self, to_process:List[str] = []) -> None:
        for local_path in self.path:
            for step in local_path:
                if (not to_process) or (step.name in to_process):
                    if step.status.to_process:
                        logger.info(f"Going to submit {len(step.status.to_process)} jobs for step {step.name}")
                        self._send_jobs(step)


    def _get_next_steps(self, i:int) -> List[Step]:
        if np.sum(self.state[i] ==  1) == 0: #Nothing to do
            return None

        last_steps = self.path[-1]
        nb_last_steps = len(last_steps)
        
        if 2 in self.state[i, :-nb_last_steps]: #Temp file before the last steps there, a job is in progress
            return None
        
        first_valid_step = np.argmax(self.state[i] == 1)

        flat_path = [s for local_path in self.path for s in local_path]

        if first_valid_step >= self.state.shape[1] - nb_last_steps: #The first valid step is part of the last meta-step
            valid_steps = np.flatnonzero(self.state[i, :] == 1)
            return [flat_path[step] for step in valid_steps]
        else:
            return [flat_path[first_valid_step]]

    def display(self, skip_ok:bool = False) -> None:
        for i in range(self.N):
            idx = 0
            if skip_ok and np.sum(self.state[i]) == 0: #All files available
                continue
            line = f"[{i}] =>"
            for local_path in self.path:
                if len(local_path) > 1:
                    line += " {"
                for step in local_path:
                    color = colormap[self.state[i, idx]]
                    line += f" {color}{step.name}{bcolors.ENDC}"
                    idx += 1
                if len(local_path) > 1:
                    line += " }"
            print(line)
        print(f"Legend: {colormap[0]}Processed{bcolors.ENDC} {colormap[2]}Running{bcolors.ENDC} {colormap[1]}Missing{bcolors.ENDC}")

    def print_process(self) -> None:
        for local_path in self.path:
            for step in local_path:
                print(f"{step.name} => {step.status.to_process}")

    def _clear_temp_single(self, step, id):
        fname = f"{step.odir}/{id}.temp"
        if not self.dry:
            try:
                os.remove(fname)
            except OSError as e:
                logger.warning(e)
            
    def _clear_temp(self, full:bool = False, to_process:List[str] = []) -> None:
        for local_path in self.path:
            for step in local_path:
                if not to_process or step.name in to_process:
                    if full:
                        new_files = step.status.temp
                    else:
                        new_files = step.status.temp.intersection(step.status.files)
                    if new_files:
                        for i in new_files:
                            self._clear_temp_single(step, i)
                            # else:
                            #     print(f"Would remove {fname}")
                        
                        step.status.temp = step.status.temp - new_files
                        print(f"Cleaned {len(new_files)} temp files for step {step.name}")

    def _create_temp(self, step:Step, i:int, jobid:str) -> None:
        if not self.dry:
            fname = f"{step.odir}/{i}.temp"
            with open(fname, 'a') as f:
                f.write(jobid)

    def _create_map(self, step:Step) -> str:
        with NamedTemporaryFile(mode='w', delete=False, suffix='.tmp', prefix='map') as handle:
            handle.write('\n'.join(map(str, step.status.to_process)))
            fname = handle.name
        return fname

    def _create_setup(self, step:Step, map_file:str) -> str:
        setup_map = {
            "DUNEVERSION": step.dune_version,
            "DUNEQUALIFIER": step.dune_qual,
            "FCL": os.path.basename(step.fcl),
            "NEVENTS": step.nevents,
            "MAP_FILE": map_file,
            "IDIR": step.idir,
            "IBASENAME": step.ifile,
            "ODIR": step.odir,
            "OBASENAME": step.ofile,
            "ADDITIONAL_OUTPUTS": step.outputs
        }

        if step.local_source != "":
            setup_map["SOURCE_FOLDER"] = os.path.basename(step.local_source).replace('.tar.gz', '')

        with NamedTemporaryFile(mode='w', delete=False, suffix='.sh', prefix='job_setup') as handle:
            to_write = []
            for key, val in setup_map.items():
                if isinstance(val, list):
                    to_write.append(f"{key}=({' '.join(val)})")
                else:
                    to_write.append(f"{key}={val}")
            handle.write('\n'.join(to_write))
            fname = handle.name
        return fname

    def reset_temp(self, to_process:List[str] = []) -> None:
        self._clear_temp(full=True, to_process=to_process)

    def _extract_jobid(self, stdout) -> str:
        expr = r'\d+\.\d+@.*\.fnal\.gov'
        return re.search(expr, stdout).group(0)

    def check_steps_exist(self, to_process:List[str]):
        available_steps = [step.name for local_path in self.path for step in local_path]
        for s in to_process:
            if s not in available_steps:
                logging.critical(f"Step {s} does not exist")
                sys.exit()

    def rebuild_db(self):
        new_rows = []
        for local_path in p.path:
            for step in local_path:
                tmp_files = [f"{step.odir}/{i}.temp" for i in step.status.temp]
                
                for i, tmp in zip(step.status.temp, tmp_files):
                    with open(tmp, 'r') as tmp:
                        jid = tmp.readline()
                    db_row = {
                        'jobid': jid,
                        'status': 'I',
                        'step': step.name,
                        'step_id': i
                    }
                    logger.info(jid)
                    new_rows.append(db_row)
        self.db = pd.DataFrame(new_rows)
        logger.info(self.db)
        save_database(self.conn, self.db)
        logger.info(f"Rebuild database. Now containing {len(self.db)} entries")

    def close_db(self):
        self.conn.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Restarts failed jobs")
    parser.add_argument('path_file')
    parser.add_argument('action', choices=['send', 'clear', 'dry', 'rebuild_db', 'new'])
    parser.add_argument('--skip-ok', action="store_true", help="Doesn't print the lines when all the files are available for a specific id")
    parser.add_argument('--steps', nargs='*', action='store', help="List of steps on which to apply the given action")
    args = parser.parse_args()

    path_file = args.path_file

    new = (args.action == 'new')

    p = Process(path_file, args.action == 'dry', new)

    if args.steps: #Check that only valid steps are given
        p.check_steps_exist(args.steps)

    # p.reset_temp()
    p.display(skip_ok=args.skip_ok)
    p.print_process()

    if args.action in ['send', 'dry']:
        p.submit(to_process=args.steps)
    elif args.action == 'clear':
        p.reset_temp(to_process=args.steps)
    elif args.action == 'rebuild_db':
        p.rebuild_db()
    
    p.close_db()
