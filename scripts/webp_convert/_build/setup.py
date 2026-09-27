"""Cython 编译入口：pan.py + run.py + gh_issue.py -> .so
GitHub Actions Linux runner 上跑。
"""
from setuptools import setup, Extension
from Cython.Build import cythonize
import Cython.Compiler.Options as opts
opts.c_string_type = 'str'
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

pan = Extension("pan", sources=["pan.py"])
run = Extension("run", sources=["run.py"])
gh = Extension("gh_issue", sources=["gh_issue.py"])

setup(
    name="webp_convert",
    ext_modules=cythonize([pan, run, gh], language_level=3),
)
