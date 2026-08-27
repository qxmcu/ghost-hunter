from setuptools import setup, find_packages

setup(
    name="ghost-hunter",
    version="1.0.0",
    packages=find_packages(),
    py_modules=["cli"],
    install_requires=[
        "fastapi",
        "uvicorn",
        "httpx",
        "httpx-sse",
        "docker",
        "pydantic",
        "pydantic-settings",
        "click"
    ],
    entry_points={
        "console_scripts": [
            "ghost = cli:cli",
        ],
    },
)
