from setuptools import setup

setup(
    name='jupyterhub-h2ospawner',
    version='0.1.10',
    description='JupyterHub Spawner using H2O',
    long_description='See https://github.com/c2j/h2ospawner for more info',
    url='https://github.com/c2j/h2ospawner',
    author='Jianjun Chen',
    author_email='chenjj.yz@gmail.com',
    license='3 Clause BSD',
    packages=['h2ospawner'],
    install_requires=['jupyterhub>=0.7'],
)
