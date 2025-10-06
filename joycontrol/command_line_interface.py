import asyncio
import inspect
import logging
import shlex

from aioconsole import ainput

from joycontrol.controller_state import button_push, ControllerState
from joycontrol.transport import NotConnectedError

logger = logging.getLogger(__name__)


def _print_doc(string):
    """
    Attempts to remove common white space at the start of the lines in a doc string
    to unify the output of doc strings with different indention levels.

    Keeps whitespace lines intact.

    :param fun: function to print the doc string of
    """
    lines = string.split('\n')
    if lines:
        prefix_i = 0
        for i, line_0 in enumerate(lines):
            # find non empty start lines
            if line_0.strip():
                # traverse line and stop if character mismatch with other non empty lines
                for prefix_i, c in enumerate(line_0):
                    if not c.isspace():
                        break
                    if any(lines[j].strip() and (prefix_i >= len(lines[j]) or c != lines[j][prefix_i])
                           for j in range(i+1, len(lines))):
                        break
                break

        for line in lines:
            print(line[prefix_i:] if line.strip() else line)


class CLI:
    def __init__(self):
        self.commands = {}

    def add_command(self, name, command):
        if name in self.commands:
            raise ValueError(f'Command {name} already registered.')
        self.commands[name] = command

    async def cmd_help(self):
        print('Commands:')
        for name, fun in inspect.getmembers(self):
            if name.startswith('cmd_') and fun.__doc__:
                _print_doc(fun.__doc__)

        for name, fun in self.commands.items():
            if fun.__doc__:
                _print_doc(fun.__doc__)

        print('Commands can be chained using "&&"')
        print('Type "exit" to close.')

    async def run(self):
        while True:
            user_input = await ainput(prompt='cmd >> ')
            if not user_input:
                continue

            for command in user_input.split('&&'):
                cmd, *args = shlex.split(command)

                if cmd == 'exit':
                    return

                if hasattr(self, f'cmd_{cmd}'):
                    try:
                        result = await getattr(self, f'cmd_{cmd}')(*args)
                        if result:
                            print(result)
                    except Exception as e:
                        print(e)
                elif cmd in self.commands:
                    try:
                        result = await self.commands[cmd](*args)
                        if result:
                            print(result)
                    except Exception as e:
                        print(e)
                else:
                    print('command', cmd, 'not found, call help for help.')

    @staticmethod
    def deprecated(message):
        async def dep_printer(*args, **kwargs):
            print(message)

        return dep_printer


class ControllerCLI(CLI):
    def __init__(self, controller_state: ControllerState):
        super().__init__()
        self.controller_state = controller_state
        # --- NUEVO: registro de tareas "mendez" por botón ---
        self._mendez_tasks = {}

    async def cmd_help(self):
        print('Button commands:')
        print(', '.join(self.controller_state.button_state.get_available_buttons()))
        print()
        print('mendez <boton> <intervalo_ms>  - inicia un bucle que pulsa <boton> cada <intervalo_ms> ms')
        print('mendez_stop <boton>            - detiene el bucle del <boton>')
        print('mendez_list                    - lista bucles activos')
        print()
        await super().cmd_help()

    @staticmethod
    def _set_stick(stick, direction, value):
        if direction == 'center':
            stick.set_center()
        elif direction == 'up':
            stick.set_up()
        elif direction == 'down':
            stick.set_down()
        elif direction == 'left':
            stick.set_left()
        elif direction == 'right':
            stick.set_right()
        elif direction in ('h', 'horizontal'):
            if value is None:
                raise ValueError(f'Missing value')
            try:
                val = int(value)
            except ValueError:
                raise ValueError(f'Unexpected stick value "{value}"')
            stick.set_h(val)
        elif direction in ('v', 'vertical'):
            if value is None:
                raise ValueError(f'Missing value')
            try:
                val = int(value)
            except ValueError:
                raise ValueError(f'Unexpected stick value "{value}"')
            stick.set_v(val)
        else:
            raise ValueError(f'Unexpected argument "{direction}"')

        return f'{stick.__class__.__name__} was set to ({stick.get_h()}, {stick.get_v()}).'

    async def cmd_stick(self, side, direction, value=None):
        """
        stick - Command to set stick positions.
        :param side: 'l', 'left' for left control stick; 'r', 'right' for right control stick
        :param direction: 'center', 'up', 'down', 'left', 'right';
                          'h', 'horizontal' or 'v', 'vertical' to set the value directly to the "value" argument
        :param value: horizontal or vertical value
        """
        if side in ('l', 'left'):
            stick = self.controller_state.l_stick_state
            return ControllerCLI._set_stick(stick, direction, value)
        elif side in ('r', 'right'):
            stick = self.controller_state.r_stick_state
            return ControllerCLI._set_stick(stick, direction, value)
        else:
            raise ValueError('Value of side must be "l", "left" or "r", "right"')

    # ---------------- NUEVO: comandos "mendez" ----------------
    async def _mendez_worker(self, button: str, interval_ms: int):
        """
        Pulsa 'button' indefinidamente cada 'interval_ms' milisegundos
        hasta que se cancele la task correspondiente.
        """
        try:
            available = self.controller_state.button_state.get_available_buttons()
            if button not in available:
                print(f'Botón "{button}" no es válido. Disponibles: {", ".join(sorted(available))}')
                return

            # Normaliza intervalo (mínimo 1 ms)
            interval = max(1, int(interval_ms)) / 1000.0

            while True:
                # Pulso corto (0.1s por defecto en button_push), luego espera el intervalo
                await button_push(self.controller_state, button)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            # Cancelación silenciosa
            pass
        except NotConnectedError:
            print('Conexión perdida durante mendez; deteniendo bucle.')
        except Exception as e:
            print(f'Error en mendez({button}): {e}')
        finally:
            # Limpieza del registro si aplica
            task = self._mendez_tasks.get(button)
            if task and task.done():
                self._mendez_tasks.pop(button, None)

    async def cmd_mendez(self, button: str, interval_ms: str):
        """
        mendez <boton> <intervalo_ms>
        Inicia un bucle infinito que pulsa <boton> cada <intervalo_ms> ms.
        """
        if button in self._mendez_tasks and not self._mendez_tasks[button].done():
            return f'Ya hay un mendez activo para "{button}". Usa: mendez_stop {button}'

        try:
            interval_int = int(interval_ms)
        except ValueError:
            raise ValueError('intervalo_ms debe ser un entero (milisegundos).')

        task = asyncio.create_task(self._mendez_worker(button, interval_int), name=f'mendez:{button}')
        self._mendez_tasks[button] = task
        return f'Iniciado mendez para "{button}" cada {interval_int} ms.'

    async def cmd_mendez_stop(self, button: str):
        """
        mendez_stop <boton>
        Detiene el bucle "mendez" del <boton>.
        """
        task = self._mendez_tasks.get(button)
        if not task or task.done():
            return f'No hay mendez activo para "{button}".'
        task.cancel()
        return f'Detenido mendez para "{button}".'

    async def cmd_mendez_list(self):
        """
        mendez_list
        Lista los bucles mendez activos.
        """
        activos = [b for b, t in self._mendez_tasks.items() if t and not t.done()]
        if not activos:
            return 'No hay mendez activos.'
        return 'Mendez activos: ' + ', '.join(sorted(activos))
    # ----------------------------------------------------------

    async def run(self):
        while True:
            user_input = await ainput(prompt='cmd >> ')
            if not user_input:
                continue

            buttons_to_push = []

            for command in user_input.split('&&'):
                cmd, *args = shlex.split(command)

                if cmd == 'exit':
                    # Al salir, cancelar todos los mendez activos
                    for b, t in list(self._mendez_tasks.items()):
                        if t and not t.done():
                            t.cancel()
                    return

                available_buttons = self.controller_state.button_state.get_available_buttons()

                if hasattr(self, f'cmd_{cmd}'):
                    try:
                        result = await getattr(self, f'cmd_{cmd}')(*args)
                        if result:
                            print(result)
                    except Exception as e:
                        print(e)
                elif cmd in self.commands:
                    try:
                        result = await self.commands[cmd](*args)
                        if result:
                            print(result)
                    except Exception as e:
                        print(e)
                elif cmd in available_buttons:
                    buttons_to_push.append(cmd)
                else:
                    print('command', cmd, 'not found, call help for help.')

            if buttons_to_push:
                await button_push(self.controller_state, *buttons_to_push)
            else:
                try:
                    await self.controller_state.send()
                except NotConnectedError:
                    logger.info('Connection was lost.')
                    # Si se perdió conexión, cancelar también los mendez activos
                    for b, t in list(self._mendez_tasks.items()):
                        if t and not t.done():
                            t.cancel()
                    return
