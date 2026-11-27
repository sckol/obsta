"""Initialize the arc67 structure on explicit request."""

from .assets import checked_folder, install_templates

SECTIONS = (
    'Presentations', 'Guardrails', 'References', 'Plans', 'Sources', 'Scenarios',
    'Configs', 'Conventions', 'Decisions', 'Reports', 'Todos', 'Glossary',
)
ARCHITECTURE = """# Architecture

## Introduction & Goals
### Requirements Overview
### Quality Goals
### Stakeholders

## Architecture Constraints

## Context & Scope

## Solution Strategy

## Building Block View

## Runtime View

## Deployment View

## Crosscutting Concepts

## Architecture Decisions

## Risks and Technical Debt

## Glossary

![[Glossary.base]]
"""
GLOSSARY = """filters:
  and:
    - file.inFolder("Glossary")
views:
  - type: table
    name: Glossary
"""


def create_once(path, content):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f'Expected a regular file: {path}')
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(content)
    except FileExistsError:
        if not path.is_file() or path.is_symlink():
            raise


def initialize(root):
    if not root.is_dir():
        raise ValueError(f'Vault directory does not exist: {root}')
    for section in SECTIONS:
        # Existing arc67 vaults may use TODOs, including on case-sensitive volumes.
        if section == 'Todos' and (root / 'TODOs').exists():
            section = 'TODOs'
        checked_folder(root, section)
    create_once(root / 'Architecture.md', ARCHITECTURE)
    create_once(root / 'Glossary.base', GLOSSARY)
    install_templates(root, explicit=True)
