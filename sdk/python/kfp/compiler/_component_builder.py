# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import tarfile
import uuid
import os
import inspect
import re
import sys
import tempfile
import logging
from collections import OrderedDict
from pathlib import Path
from ..components._components import _create_task_factory_from_component_spec

class VersionedDependency(object):
  """ DependencyVersion specifies the versions """
  def __init__(self, name, version=None, min_version=None, max_version=None):
    """ if version is specified, no need for min_version or max_version;
     if both are specified, version is adopted """
    self._name = name
    if version is not None:
      self._min_version = version
      self._max_version = version
    else:
      self._min_version = min_version
      self._max_version = max_version

  @property
  def name(self):
    return self._name

  @property
  def min_version(self):
    return self._min_version

  @min_version.setter
  def min_version(self, min_version):
    self._min_version = min_version

  def has_min_version(self):
    return self._min_version != None

  @property
  def max_version(self):
    return self._max_version

  @max_version.setter
  def max_version(self, max_version):
    self._max_version = max_version

  def has_max_version(self):
    return self._max_version != None

  def has_versions(self):
    return (self.has_min_version()) or (self.has_max_version())

class DependencyHelper(object):
  """ DependencyHelper manages software dependency information """
  def __init__(self):
    self._PYTHON_PACKAGE = 'PYTHON_PACKAGE'
    self._dependency = {self._PYTHON_PACKAGE:OrderedDict()}

  @property
  def python_packages(self):
    return self._dependency[self._PYTHON_PACKAGE]

  def add_python_package(self, dependency, override=True):
    """ add_single_python_package adds a dependency for the python package

    Args:
      name: package name
      version: it could be a specific version(1.10.0), or a range(>=1.0,<=2.0)
        if not specified, the default is resolved automatically by the pip system.
      override: whether to override the version if already existing in the dependency.
    """
    if dependency.name in self.python_packages and not override:
      return
    self.python_packages[dependency.name] = dependency

  def generate_pip_requirements(self, target_file):
    """ write the python packages to a requirement file
    the generated file follows the order of which the packages are added """
    with open(target_file, 'w') as f:
      for name, version in self.python_packages.items():
        version_str = ''
        if version.has_min_version():
          version_str += ' >= ' + version.min_version + ','
        if version.has_max_version():
          version_str += ' <= ' + version.max_version + ','
        f.write(name + version_str.rstrip(',') + '\n')

def _dependency_to_requirements(dependency=[], filename='requirements.txt'):
  """
    Generates a requirement file based on the dependency
    Args:
      dependency (list): a list of VersionedDependency, which includes the package name and versions
      filename (str): requirement file name, default as requirements.txt
  """
  dependency_helper = DependencyHelper()
  for version in dependency:
    dependency_helper.add_python_package(version)
  dependency_helper.generate_pip_requirements(filename)

def _generate_dockerfile(filename, base_image, entrypoint_filename, python_version, requirement_filename=None):
  """
    generates dockerfiles
    Args:
      filename (str): target file name for the dockerfile.
      base_image (str): the base image name.
      entrypoint_filename (str): the path of the entrypoint source file that is copied to the docker image.
      python_version (str): choose python2 or python3
      requirement_filename (str): requirement file name
  """
  if python_version not in ['python2', 'python3']:
    raise ValueError('python_version has to be either python2 or python3')
  with open(filename, 'w') as f:
    f.write('FROM ' + base_image + '\n')
    if python_version is 'python3':
      f.write('RUN apt-get update -y && apt-get install --no-install-recommends -y -q python3 python3-pip python3-setuptools\n')
    else:
      f.write('RUN apt-get update -y && apt-get install --no-install-recommends -y -q python python-pip python-setuptools\n')
    if requirement_filename is not None:
      f.write('ADD ' + requirement_filename + ' /ml/requirements.txt\n')
      if python_version is 'python3':
        f.write('RUN pip3 install -r /ml/requirements.txt\n')
      else:
        f.write('RUN pip install -r /ml/requirements.txt\n')
    f.write('ADD ' + entrypoint_filename + ' /ml/main.py\n')
    if python_version is 'python3':
      f.write('ENTRYPOINT ["python3", "-u", "/ml/main.py"]')
    else:
      f.write('ENTRYPOINT ["python", "-u", "/ml/main.py"]')

class CodeGenerator(object):
  """ CodeGenerator helps to generate python codes with identation """
  def __init__(self, indentation='\t'):
    self._indentation = indentation
    self._code = []
    self._level = 0

  def begin(self):
    self._code = []
    self._level = 0

  def indent(self):
    self._level += 1

  def dedent(self):
    if self._level == 0:
      raise Exception('CodeGenerator dedent error')
    self._level -= 1

  def writeline(self, line):
    self._code.append(self._indentation * self._level + line)

  def end(self):
    line_sep = '\n'
    return line_sep.join(self._code) + line_sep

def _func_to_entrypoint(component_func, python_version='python3'):
  '''
  args:
    python_version (str): choose python2 or python3, default is python3
  '''
  if python_version not in ['python2', 'python3']:
    raise ValueError('python_version has to be either python2 or python3')

  fullargspec = inspect.getfullargspec(component_func)
  annotations = fullargspec[6]
  input_args = fullargspec[0]
  inputs = {}
  output = None
  if 'return' in annotations.keys():
    output = annotations['return']
  output_is_named_tuple = hasattr(output, '_fields')
  
  for key, value in annotations.items():
    if key != 'return':
      inputs[key] = value
  if len(input_args) != len(inputs):
    raise Exception('Some input arguments do not contain annotations.')
  if 'return' in  annotations and annotations['return'] not in [int, 
        float, str, bool] and not output_is_named_tuple:
    raise Exception('Output type not supported and supported types are [int, float, str, bool]')
  if output_is_named_tuple:
      types = output._field_types
      for field in output._fields: #Make sure all elements are supported
        if types[field] not in [int, float, str, bool]:
          raise Exception('Output type not supported and supported types are [int, float, str, bool]')
  
  # inputs is a dictionary with key of argument name and value of type class
  # output is a type class, e.g. int, str, bool, float, NamedTuple.

  # Follow the same indentation with the component source codes.
  component_src = inspect.getsource(component_func)
  match = re.search(r'\n([ \t]+)[\w]+', component_src)
  indentation = match.group(1) if match else '\t'
  codegen = CodeGenerator(indentation=indentation)

  # Function signature
  new_func_name = 'wrapper_' + component_func.__name__
  codegen.begin()
  func_signature = 'def ' + new_func_name + '('
  for input_arg in input_args:
    func_signature += input_arg + ','
  func_signature = func_signature + '_output_files' if output_is_named_tuple else func_signature + '_output_file'
  func_signature += '):'
  codegen.writeline(func_signature)

  # Call user function
  codegen.indent()
  call_component_func = 'output = ' + component_func.__name__ + '('
  if output_is_named_tuple:
    call_component_func = call_component_func.replace('output', 'outputs')
  for input_arg in input_args:
    call_component_func += inputs[input_arg].__name__ + '(' + input_arg + '),'
  call_component_func = call_component_func.rstrip(',')
  call_component_func += ')'
  codegen.writeline(call_component_func)

  # Serialize output
  codegen.writeline('import os')
  if output_is_named_tuple:
    codegen.writeline('for _output_file, output in zip(_output_files, outputs):')
    codegen.indent()
  codegen.writeline('os.makedirs(os.path.dirname(_output_file))')
  codegen.writeline('with open(_output_file, "w") as data:')
  codegen.indent()
  codegen.writeline('data.write(str(output))')
  wrapper_code = codegen.end()

  # CLI codes
  codegen.begin()
  codegen.writeline('import argparse')
  codegen.writeline('parser = argparse.ArgumentParser(description="Parsing arguments")')
  for input_arg in input_args:
    codegen.writeline('parser.add_argument("' + input_arg + '", type=' + inputs[input_arg].__name__ + ')')
  if output_is_named_tuple:
    codegen.writeline('parser.add_argument("_output_files", type=str, nargs=' + str(len(annotations['return']._fields)) + ')')
  else:
    codegen.writeline('parser.add_argument("_output_file", type=str)')
  codegen.writeline('args = vars(parser.parse_args())')
  codegen.writeline('')
  codegen.writeline('if __name__ == "__main__":')
  codegen.indent()
  codegen.writeline(new_func_name + '(**args)')

  # Remove the decorator from the component source
  src_lines = component_src.split('\n')
  start_line_num = 0
  for line in src_lines:
    if line.startswith('def '):
      break
    start_line_num += 1
  if python_version == 'python2':
    src_lines[start_line_num] = 'def ' + component_func.__name__ + '(' + ', '.join((inspect.getfullargspec(component_func).args)) + '):'
  dedecorated_component_src = '\n'.join(src_lines[start_line_num:])
  if output_is_named_tuple:
    dedecorated_component_src = 'from typing import NamedTuple\n' + dedecorated_component_src

  complete_component_code = dedecorated_component_src + '\n' + wrapper_code + '\n' + codegen.end()
  return complete_component_code

class ImageBuilder(object):
  """ Component Builder. """
  def __init__(self, gcs_base, target_image):
    self._arc_docker_filename = 'dockerfile'
    self._arc_python_filename = 'main.py'
    self._arc_requirement_filename = 'requirements.txt'
    self._tarball_filename = str(uuid.uuid4()) + '.tar.gz'
    self._gcs_base = gcs_base
    if not self._check_gcs_path(self._gcs_base):
      raise Exception('ImageBuild __init__ failure.')
    self._gcs_path = os.path.join(self._gcs_base, self._tarball_filename)
    self._target_image = target_image

  def _wrap_files_in_tarball(self, tarball_path, files={}):
    """ _wrap_files_in_tarball creates a tarball for all the input files
    with the filename configured as the key of files """
    if not tarball_path.endswith('.tar.gz'):
      raise ValueError('the tarball path should end with .tar.gz')
    with tarfile.open(tarball_path, 'w:gz') as tarball:
      for key, value in files.items():
        tarball.add(value, arcname=key)

  def _prepare_buildfiles(self, local_tarball_path, docker_filename, python_filename=None, requirement_filename=None):
    """ _prepare_buildfiles generates the tarball with all the build files
    Args:
      local_tarball_path (str): generated tarball file
      docker_filename (str): docker filename
      python_filename (str): python filename
      requirement_filename (str): requirement filename
    """
    file_lists =  {self._arc_docker_filename:docker_filename}
    if python_filename is not None:
      file_lists[self._arc_python_filename] = python_filename
    if requirement_filename is not None:
      file_lists[self._arc_requirement_filename] = requirement_filename
    self._wrap_files_in_tarball(local_tarball_path, file_lists)

  def _check_gcs_path(self, gcs_path):
    """ _check_gcs_path check both the path validity and write permissions """
    logging.info('Checking path: {}...'.format(gcs_path))
    if not gcs_path.startswith('gs://'):
      logging.error('Error: {} should be a GCS path.'.format(gcs_path))
      return False
    return True

  def _generate_kaniko_spec(self, namespace, arc_dockerfile_name, gcs_path, target_image):
    """_generate_kaniko_yaml generates kaniko job yaml based on a template yaml """
    content = {
      'apiVersion': 'v1',
      'metadata': {
        'generateName': 'kaniko-',
        'namespace': namespace,
      },
      'kind': 'Pod',
      'spec': {
        'restartPolicy': 'Never',
        'containers': [{
          'name': 'kaniko',
          'args': ['--cache=true',
                   '--dockerfile=' + arc_dockerfile_name,
                   '--context=' + gcs_path,
                   '--destination=' + target_image],
          'image': 'gcr.io/kaniko-project/executor@sha256:78d44ec4e9cb5545d7f85c1924695c89503ded86a59f92c7ae658afa3cff5400',
          'env': [{
            'name': 'GOOGLE_APPLICATION_CREDENTIALS',
            'value': '/secret/gcp-credentials/user-gcp-sa.json'
          }],
          'volumeMounts': [{
            'mountPath': '/secret/gcp-credentials',
            'name': 'gcp-credentials',
          }],
        }],
        'volumes': [{
          'name': 'gcp-credentials',
          'secret': {
            'secretName': 'user-gcp-sa',
          },
        }],
        'serviceAccountName': 'default'}
    }
    return content

  def _build_image(self, local_tarball_path, namespace, timeout):
    from ._gcs_helper import GCSHelper
    GCSHelper.upload_gcs_file(local_tarball_path, self._gcs_path)
    kaniko_spec = self._generate_kaniko_spec(namespace=namespace,
                                             arc_dockerfile_name=self._arc_docker_filename,
                                             gcs_path=self._gcs_path,
                                             target_image=self._target_image)
    # Run kaniko job
    logging.info('Start a kaniko job for build.')
    from ._k8s_helper import K8sHelper
    k8s_helper = K8sHelper()
    k8s_helper.run_job(kaniko_spec, timeout)
    logging.info('Kaniko job complete.')

    # Clean up
    GCSHelper.remove_gcs_blob(self._gcs_path)

  def build_image_from_func(self, component_func, namespace, base_image, timeout, dependency, python_version='python3'):
    """ build_image builds an image for the given python function
    args:
      python_version (str): choose python2 or python3, default is python3
    """
    if python_version not in ['python2', 'python3']:
      raise ValueError('python_version has to be either python2 or python3')
    with tempfile.TemporaryDirectory() as local_build_dir:
      # Generate entrypoint and serialization python codes
      local_python_filepath = os.path.join(local_build_dir, self._arc_python_filename)
      logging.info('Generate entrypoint and serialization codes.')
      complete_component_code = _func_to_entrypoint(component_func, python_version)
      with open(local_python_filepath, 'w') as f:
        f.write(complete_component_code)

      local_requirement_filepath = os.path.join(local_build_dir, self._arc_requirement_filename)
      logging.info('Generate requirement file')
      _dependency_to_requirements(dependency, local_requirement_filepath)

      local_docker_filepath = os.path.join(local_build_dir, self._arc_docker_filename)
      _generate_dockerfile(local_docker_filepath, base_image, self._arc_python_filename, python_version, self._arc_requirement_filename)

      # Prepare build files
      logging.info('Generate build files.')
      local_tarball_path = os.path.join(local_build_dir, 'docker.tmp.tar.gz')
      self._prepare_buildfiles(local_tarball_path, local_docker_filepath, local_python_filepath, local_requirement_filepath)
      self._build_image(local_tarball_path, namespace, timeout)

  def build_image_from_dockerfile(self, docker_filename, timeout, namespace):
    """ build_image_from_dockerfile builds an image based on the dockerfile """
    with tempfile.TemporaryDirectory() as local_build_dir:
      # Prepare build files
      logging.info('Generate build files.')
      local_tarball_path = os.path.join(local_build_dir, 'docker.tmp.tar.gz')
      self._prepare_buildfiles(local_tarball_path, docker_filename=docker_filename)
      self._build_image(local_tarball_path, namespace, timeout)

def _configure_logger(logger):
  """ _configure_logger configures the logger such that the info level logs
  go to the stdout and the error(or above) level logs go to the stderr.
  It is important for the Jupyter notebook log rendering """
  if hasattr(_configure_logger, 'configured'):
    # Skip the logger configuration the second time this function
    # is called to avoid multiple streamhandlers bound to the logger.
    return
  setattr(_configure_logger, 'configured', 'true')
  logger.setLevel(logging.INFO)
  info_handler = logging.StreamHandler(stream=sys.stdout)
  info_handler.addFilter(lambda record: record.levelno <= logging.INFO)
  info_handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
  error_handler = logging.StreamHandler(sys.stderr)
  error_handler.addFilter(lambda record: record.levelno > logging.INFO)
  error_handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
  logger.addHandler(info_handler)
  logger.addHandler(error_handler)

def _generate_pythonop(component_func, target_image, target_component_file=None):
  """ Generate operator for the pipeline authors
  The returned value is in fact a function, which should generates a container_op instance. """

  from ..components._python_op import _python_function_name_to_component_name
  from ..components._structures import InputSpec, InputValuePlaceholder, OutputPathPlaceholder, OutputSpec, ContainerImplementation, ContainerSpec, ComponentSpec


  #Component name and description are derived from the function's name and docstribng, but can be overridden by @python_component function decorator
  #The decorator can set the _component_human_name and _component_description attributes. getattr is needed to prevent error when these attributes do not exist.
  component_name = getattr(component_func, '_component_human_name', None) or _python_function_name_to_component_name(component_func.__name__)
  component_description = getattr(component_func, '_component_description', None) or (component_func.__doc__.strip() if component_func.__doc__ else None)

  #TODO: Humanize the input/output names
  input_names = inspect.getfullargspec(component_func)[0]

  return_ann = inspect.signature(component_func).return_annotation
  output_is_named_tuple = hasattr(return_ann, '_fields')

  output_names = ['output']
  if output_is_named_tuple:
    output_names = return_ann._fields

  component_spec = ComponentSpec(
      name=component_name,
      description=component_description,
      inputs=[InputSpec(name=input_name, type='str') for input_name in input_names], #TODO: Change type to actual type
       outputs=[OutputSpec(name=output_name, type='str') for output_name in output_names],
      implementation=ContainerImplementation(
          container=ContainerSpec(
              image=target_image,
              #command=['python3', program_file], #TODO: Include the command line
              args=[InputValuePlaceholder(input_name) for input_name in input_names] + 
                [OutputPathPlaceholder(output_name) for output_name in output_names],
          )
      )
  )
  
  target_component_file = target_component_file or getattr(component_func, '_component_target_component_file', None)
  if target_component_file:
    from ..components._yaml_utils import dump_yaml
    component_text = dump_yaml(component_spec.to_dict())
    Path(target_component_file).write_text(component_text)

  return _create_task_factory_from_component_spec(component_spec)

def build_python_component(component_func, target_image, base_image=None, dependency=[], staging_gcs_path=None, build_image=True, timeout=600, namespace='kubeflow', target_component_file=None, python_version='python3'):
  """ build_component automatically builds a container image for the component_func
  based on the base_image and pushes to the target_image.

  Args:
    component_func (python function): The python function to build components upon
    base_image (str): Docker image to use as a base image
    target_image (str): Full URI to push the target image
    staging_gcs_path (str): GCS blob that can store temporary build files
    target_image (str): target image path
    build_image (bool): whether to build the image or not. Default is True.
    timeout (int): the timeout for the image build(in secs), default is 600 seconds
    namespace (str): the namespace within which to run the kubernetes kaniko job, default is "kubeflow"
    dependency (list): a list of VersionedDependency, which includes the package name and versions, default is empty
    python_version (str): choose python2 or python3, default is python3
  Raises:
    ValueError: The function is not decorated with python_component decorator or the python_version is neither python2 nor python3
  """

  _configure_logger(logging.getLogger())

  if component_func is None:
    raise ValueError('component_func must not be None')
  if target_image is None:
    raise ValueError('target_image must not be None')

  if python_version not in ['python2', 'python3']:
    raise ValueError('python_version has to be either python2 or python3')

  if build_image:
    if staging_gcs_path is None:
      raise ValueError('staging_gcs_path must not be None')

    if base_image is None:
      base_image = getattr(component_func, '_component_base_image', None)
    if base_image is None:
      raise ValueError('base_image must not be None')

    logging.info('Build an image that is based on ' +
                                   base_image +
                                   ' and push the image to ' +
                                   target_image)
    builder = ImageBuilder(gcs_base=staging_gcs_path, target_image=target_image)
    builder.build_image_from_func(component_func, namespace=namespace,
                                  base_image=base_image, timeout=timeout,
                                  python_version=python_version, dependency=dependency)
    logging.info('Build component complete.')
  return _generate_pythonop(component_func, target_image, target_component_file)

def build_docker_image(staging_gcs_path, target_image, dockerfile_path, timeout=600, namespace='kubeflow'):
  """ build_docker_image automatically builds a container image based on the specification in the dockerfile and
  pushes to the target_image.

  Args:
    staging_gcs_path (str): GCS blob that can store temporary build files
    target_image (str): gcr path to push the final image
    dockerfile_path (str): local path to the dockerfile
    timeout (int): the timeout for the image build(in secs), default is 600 seconds
    namespace (str): the namespace within which to run the kubernetes kaniko job, default is "kubeflow"
  """
  _configure_logger(logging.getLogger())
  builder = ImageBuilder(gcs_base=staging_gcs_path, target_image=target_image)
  builder.build_image_from_dockerfile(docker_filename=dockerfile_path, timeout=timeout, namespace=namespace)
  logging.info('Build image complete.')
