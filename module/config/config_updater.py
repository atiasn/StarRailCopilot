import re
from copy import deepcopy

from cached_property import cached_property

from deploy.Windows.utils import DEPLOY_TEMPLATE, poor_yaml_read, poor_yaml_write
from module.base.timer import timer
from module.config.server import to_server, to_package, VALID_PACKAGE, VALID_CHANNEL_PACKAGE
from module.config.utils import *

CONFIG_IMPORT = '''
import datetime

# This file was automatically generated by module/config/config_updater.py.
# Don't modify it manually.


class GeneratedConfig:
    """
    Auto generated configuration
    """
'''.strip().split('\n')

DICT_GUI_TO_INGAME = {
    'zh-CN': 'cn',
    'en-US': 'en',
    'ja-JP': 'jp',
    'zh-TW': 'cht',
}


def gui_lang_to_ingame_lang(lang: str) -> str:
    return DICT_GUI_TO_INGAME.get(lang, 'en')


class ConfigGenerator:
    @cached_property
    def argument(self):
        """
        Load argument.yaml, and standardise its structure.

        <group>:
            <argument>:
                type: checkbox|select|textarea|input
                value:
                option (Optional): Options, if argument has any options.
                validate (Optional): datetime
        """
        data = {}
        raw = read_file(filepath_argument('argument'))
        for path, value in deep_iter(raw, depth=2):
            arg = {
                'type': 'input',
                'value': '',
                # option
            }
            if not isinstance(value, dict):
                value = {'value': value}
            arg['type'] = data_to_type(value, arg=path[1])
            if isinstance(value['value'], datetime):
                arg['type'] = 'datetime'
                arg['validate'] = 'datetime'
            # Manual definition has the highest priority
            arg.update(value)
            deep_set(data, keys=path, value=arg)

        # Define storage group
        # arg = {
        #     'type': 'storage',
        #     'value': {},
        #     'valuetype': 'ignore',
        #     'display': 'disabled',
        # }
        # deep_set(data, keys=['Storage', 'Storage'], value=arg)
        return data

    @cached_property
    def task(self):
        """
        <task_group>:
            <task>:
                <group>:
        """
        return read_file(filepath_argument('task'))

    @cached_property
    def default(self):
        """
        <task>:
            <group>:
                <argument>: value
        """
        return read_file(filepath_argument('default'))

    @cached_property
    def override(self):
        """
        <task>:
            <group>:
                <argument>: value
        """
        return read_file(filepath_argument('override'))

    @cached_property
    def gui(self):
        """
        <i18n_group>:
            <i18n_key>: value, value is None
        """
        return read_file(filepath_argument('gui'))

    @cached_property
    @timer
    def args(self):
        """
        Merge definitions into standardised json.

            task.yaml ---+
        argument.yaml ---+-----> args.json
        override.yaml ---+
         default.yaml ---+

        """
        # Construct args
        data = {}
        for path, groups in deep_iter(self.task, depth=3):
            if 'tasks' not in path:
                continue
            task = path[2]
            # Add storage to all task
            # groups.append('Storage')
            for group in groups:
                if group not in self.argument:
                    print(f'`{task}.{group}` is not related to any argument group')
                    continue
                deep_set(data, keys=[task, group], value=deepcopy(self.argument[group]))

        def check_override(path, value):
            # Check existence
            old = deep_get(data, keys=path, default=None)
            if old is None:
                print(f'`{".".join(path)}` is not a existing argument')
                return False
            # Check type
            # But allow `Interval` to be different
            old_value = old.get('value', None) if isinstance(old, dict) else old
            value = old.get('value', None) if isinstance(value, dict) else value
            if type(value) != type(old_value) \
                    and old_value is not None \
                    and path[2] not in ['SuccessInterval', 'FailureInterval']:
                print(
                    f'`{value}` ({type(value)}) and `{".".join(path)}` ({type(old_value)}) are in different types')
                return False
            # Check option
            if isinstance(old, dict) and 'option' in old:
                if value not in old['option']:
                    print(f'`{value}` is not an option of argument `{".".join(path)}`')
                    return False
            return True

        # Set defaults
        for p, v in deep_iter(self.default, depth=3):
            if not check_override(p, v):
                continue
            deep_set(data, keys=p + ['value'], value=v)
        # Override non-modifiable arguments
        for p, v in deep_iter(self.override, depth=3):
            if not check_override(p, v):
                continue
            if isinstance(v, dict):
                if deep_get(v, keys='type') in ['lock']:
                    deep_default(v, keys='display', value="disabled")
                elif deep_get(v, keys='value') is not None:
                    deep_default(v, keys='display', value='hide')
                for arg_k, arg_v in v.items():
                    deep_set(data, keys=p + [arg_k], value=arg_v)
            else:
                deep_set(data, keys=p + ['value'], value=v)
                deep_set(data, keys=p + ['display'], value='hide')
        # Set command
        for path, groups in deep_iter(self.task, depth=3):
            if 'tasks' not in path:
                continue
            task = path[2]
            if deep_get(data, keys=f'{task}.Scheduler.Command'):
                deep_set(data, keys=f'{task}.Scheduler.Command.value', value=task)
                deep_set(data, keys=f'{task}.Scheduler.Command.display', value='hide')

        return data

    @timer
    def generate_code(self):
        """
        Generate python code.

        args.json ---> config_generated.py

        """
        visited_group = set()
        visited_path = set()
        lines = CONFIG_IMPORT
        for path, data in deep_iter(self.argument, depth=2):
            group, arg = path
            if group not in visited_group:
                lines.append('')
                lines.append(f'    # Group `{group}`')
                visited_group.add(group)

            option = ''
            if 'option' in data and data['option']:
                option = '  # ' + ', '.join([str(opt) for opt in data['option']])
            path = '.'.join(path)
            lines.append(f'    {path_to_arg(path)} = {repr(parse_value(data["value"], data=data))}{option}')
            visited_path.add(path)

        with open(filepath_code(), 'w', encoding='utf-8', newline='') as f:
            for text in lines:
                f.write(text + '\n')

    @timer
    def generate_i18n(self, lang):
        """
        Load old translations and generate new translation file.

                     args.json ---+-----> i18n/<lang>.json
        (old) i18n/<lang>.json ---+

        """
        new = {}
        old = read_file(filepath_i18n(lang))

        def deep_load(keys, default=True, words=('name', 'help')):
            for word in words:
                k = keys + [str(word)]
                d = ".".join(k) if default else str(word)
                v = deep_get(old, keys=k, default=d)
                deep_set(new, keys=k, value=v)

        # Menu
        for path, data in deep_iter(self.task, depth=3):
            if 'tasks' not in path:
                continue
            task_group, _, task = path
            deep_load(['Menu', task_group])
            deep_load(['Task', task])
        # Arguments
        visited_group = set()
        for path, data in deep_iter(self.argument, depth=2):
            if path[0] not in visited_group:
                deep_load([path[0], '_info'])
                visited_group.add(path[0])
            deep_load(path)
            if 'option' in data:
                deep_load(path, words=data['option'], default=False)

        # Package names
        for package, server in VALID_PACKAGE.items():
            path = ['Emulator', 'PackageName', package]
            if deep_get(new, keys=path) == package:
                deep_set(new, keys=path, value=server.upper())
        for package, server_and_channel in VALID_CHANNEL_PACKAGE.items():
            server, channel = server_and_channel
            name = deep_get(new, keys=['Emulator', 'PackageName', to_package(server)])
            if lang == SERVER_TO_LANG[server]:
                value = f'{name} {channel}渠道服 {package}'
            else:
                value = f'{name} {package}'
            deep_set(new, keys=['Emulator', 'PackageName', package], value=value)
        # Game server names
        # for server, _list in VALID_SERVER_LIST.items():
        #     for index in range(len(_list)):
        #         path = ['Emulator', 'ServerName', f'{server}-{index}']
        #         prefix = server.split('_')[0].upper()
        #         prefix = '国服' if prefix == 'CN' else prefix
        #         deep_set(new, keys=path, value=f'[{prefix}] {_list[index]}')

        # Dungeon names
        if lang not in ['zh-CN', 'zh-TW', 'en-US']:
            ingame_lang = gui_lang_to_ingame_lang(lang)
            from tasks.dungeon.keywords import DungeonList
            dailies = deep_get(self.argument, keys='Dungeon.Name.option')
            for dungeon in DungeonList.instances.values():
                if dungeon.name in dailies:
                    value = dungeon.__getattribute__(ingame_lang)
                    deep_set(new, keys=['Dungeon', 'Name', dungeon.name], value=value)

        # GUI i18n
        for path, _ in deep_iter(self.gui, depth=2):
            group, key = path
            deep_load(keys=['Gui', group], words=(key,))

        write_file(filepath_i18n(lang), new)

    @cached_property
    def menu(self):
        """
        Generate menu definitions

        task.yaml --> menu.json

        """
        data = {}
        for task_group in self.task.keys():
            value = deep_get(self.task, keys=[task_group, 'menu'])
            if value not in ['collapse', 'list']:
                value = 'collapse'
            deep_set(data, keys=[task_group, 'menu'], value=value)
            value = deep_get(self.task, keys=[task_group, 'page'])
            if value not in ['setting', 'tool']:
                value = 'setting'
            deep_set(data, keys=[task_group, 'page'], value=value)
            tasks = deep_get(self.task, keys=[task_group, 'tasks'], default={})
            tasks = list(tasks.keys())
            deep_set(data, keys=[task_group, 'tasks'], value=tasks)

        return data

    @staticmethod
    def generate_deploy_template():
        template = poor_yaml_read(DEPLOY_TEMPLATE)
        cn = {
            'Repository': 'https://e.coding.net/llop18870/alas/AzurLaneAutoScript.git',
            'PypiMirror': 'https://pypi.tuna.tsinghua.edu.cn/simple',
            'Language': 'zh-CN',
        }
        aidlux = {
            'GitExecutable': '/usr/bin/git',
            'PythonExecutable': '/usr/bin/python',
            'RequirementsFile': './deploy/AidLux/0.92/requirements.txt',
            'AdbExecutable': '/usr/bin/adb',
        }

        docker = {
            'GitExecutable': '/usr/bin/git',
            'PythonExecutable': '/usr/local/bin/python',
            'RequirementsFile': './deploy/docker/requirements.txt',
            'AdbExecutable': '/usr/bin/adb',
        }

        def update(suffix, *args):
            file = f'./config/deploy.{suffix}.yaml'
            new = deepcopy(template)
            for dic in args:
                new.update(dic)
            poor_yaml_write(data=new, file=file)

        update('template')
        update('template-cn', cn)
        # update('template-AidLux', aidlux)
        # update('template-AidLux-cn', aidlux, cn)
        # update('template-docker', docker)
        # update('template-docker-cn', docker, cn)

    def insert_dungeon(self):
        from tasks.dungeon.keywords import DungeonList
        dungeons = [dungeon.name for dungeon in DungeonList.instances.values() if dungeon.is_daily_dungeon]
        deep_set(self.argument, keys='Dungeon.Name.option', value=dungeons)
        deep_set(self.args, keys='Dungeon.Dungeon.Name.option', value=dungeons)
    
    def insert_assignment(self):
        from tasks.assignment.keywords import AssignmentEntry
        assignments = [entry.name for entry in AssignmentEntry.instances.values()]
        for i in range(4):
            deep_set(self.argument, keys=f'Assignment.Name_{i+1}.option', value=assignments)
            deep_set(self.args, keys=f'Assignment.Assignment.Name_{i+1}.option', value=assignments)

    def insert_package(self):
        option = deep_get(self.argument, keys='Emulator.PackageName.option')
        option += list(VALID_PACKAGE.keys())
        option += list(VALID_CHANNEL_PACKAGE.keys())
        deep_set(self.argument, keys='Emulator.PackageName.option', value=option)
        deep_set(self.args, keys='Alas.Emulator.PackageName.option', value=option)

    @timer
    def generate(self):
        _ = self.args
        _ = self.menu
        # _ = self.event
        self.insert_dungeon()
        self.insert_assignment()
        self.insert_package()
        # self.insert_server()
        write_file(filepath_args(), self.args)
        write_file(filepath_args('menu'), self.menu)
        self.generate_code()
        for lang in LANGUAGES:
            self.generate_i18n(lang)
        self.generate_deploy_template()


class ConfigUpdater:
    # source, target, (optional)convert_func
    redirection = [
    ]

    @cached_property
    def args(self):
        return read_file(filepath_args())

    def config_update(self, old, is_template=False):
        """
        Args:
            old (dict):
            is_template (bool):

        Returns:
            dict:
        """
        new = {}

        def deep_load(keys):
            data = deep_get(self.args, keys=keys, default={})
            value = deep_get(old, keys=keys, default=data['value'])
            if is_template or value is None or value == '' or data['type'] == 'lock' or data.get('display') == 'hide':
                value = data['value']
            value = parse_value(value, data=data)
            deep_set(new, keys=keys, value=value)

        for path, _ in deep_iter(self.args, depth=3):
            deep_load(path)

        if not is_template:
            new = self.config_redirect(old, new)

        return new

    def config_redirect(self, old, new):
        """
        Convert old settings to the new.

        Args:
            old (dict):
            new (dict):

        Returns:
            dict:
        """
        for row in self.redirection:
            if len(row) == 2:
                source, target = row
                update_func = None
            elif len(row) == 3:
                source, target, update_func = row
            else:
                continue

            if isinstance(source, tuple):
                value = []
                error = False
                for attribute in source:
                    tmp = deep_get(old, keys=attribute)
                    if tmp is None:
                        error = True
                        continue
                    value.append(tmp)
                if error:
                    continue
            else:
                value = deep_get(old, keys=source)
                if value is None:
                    continue

            if update_func is not None:
                value = update_func(value)

            if isinstance(target, tuple):
                for k, v in zip(target, value):
                    # Allow update same key
                    if (deep_get(old, keys=k) is None) or (source == target):
                        deep_set(new, keys=k, value=v)
            elif (deep_get(old, keys=target) is None) or (source == target):
                deep_set(new, keys=target, value=value)

        return new

    def read_file(self, config_name, is_template=False):
        """
        Read and update config file.

        Args:
            config_name (str): ./config/{file}.json
            is_template (bool):

        Returns:
            dict:
        """
        old = read_file(filepath_config(config_name))
        new = self.config_update(old, is_template=is_template)
        # The updated config did not write into file, although it doesn't matters.
        # Commented for performance issue
        # self.write_file(config_name, new)
        return new

    @staticmethod
    def write_file(config_name, data, mod_name='alas'):
        """
        Write config file.

        Args:
            config_name (str): ./config/{file}.json
            data (dict):
            mod_name (str):
        """
        write_file(filepath_config(config_name, mod_name), data)

    @timer
    def update_file(self, config_name, is_template=False):
        """
        Read, update and write config file.

        Args:
            config_name (str): ./config/{file}.json
            is_template (bool):

        Returns:
            dict:
        """
        data = self.read_file(config_name, is_template=is_template)
        self.write_file(config_name, data)
        return data


if __name__ == '__main__':
    """
    Process the whole config generation.

                 task.yaml -+----------------> menu.json
             argument.yaml -+-> args.json ---> config_generated.py
             override.yaml -+       |
                  gui.yaml --------\|
                                   ||
    (old) i18n/<lang>.json --------\\========> i18n/<lang>.json
    (old)    template.json ---------\========> template.json
    """
    # Ensure running in Alas root folder
    import os

    os.chdir(os.path.join(os.path.dirname(__file__), '../../'))

    ConfigGenerator().generate()
    ConfigUpdater().update_file('template', is_template=True)
