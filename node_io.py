"""YAML input validation shared by the node generator and trusted-node manager."""

from pathlib import Path

import yaml


class UniqueKeyLoader(yaml.SafeLoader):
    def flatten_mapping(self, node):
        # Inspect explicit keys before resolving <<. Inherited values may
        # legitimately be overridden (the generated chain templates do this).
        if not getattr(node, '_keys_checked', False):
            seen = {}
            for key_node, _ in node.value:
                key = ('<<' if key_node.tag == 'tag:yaml.org,2002:merge'
                       else self.construct_object(key_node))
                try:
                    previous = seen.get(key)
                except TypeError:
                    raise ValueError(f'YAML 映射键必须是标量，行 {key_node.start_mark.line + 1}') from None
                if previous is not None:
                    raise ValueError(
                        f'YAML 重复字段，行 {key_node.start_mark.line + 1}'
                        f'（首次出现在行 {previous}）'
                    )
                seen[key] = key_node.start_mark.line + 1
            node._keys_checked = True
        super().flatten_mapping(node)


def load_yaml(path: Path):
    try:
        return yaml.load(path.read_text(encoding='utf-8-sig'), Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, 'problem_mark', None)
        location = f'，行 {mark.line + 1}，列 {mark.column + 1}' if mark else ''
        # PyYAML's default exception text can include a password-bearing line.
        raise ValueError(f'{path}: 不是有效 YAML{location}') from None
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, UnicodeError):
            raise ValueError(f'{path}: YAML 必须使用 UTF-8 编码') from None
        raise ValueError(f'{path}: {exc}') from None
