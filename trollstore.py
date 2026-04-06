import platform
import sys
import traceback
from pathlib import Path

import click
from packaging.version import parse as parse_version
from pymobiledevice3.cli.cli_common import Command
from pymobiledevice3.exceptions import NoDeviceConnectedError, PyMobileDevice3Exception
from pymobiledevice3.lockdown import LockdownClient
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.installation_proxy import InstallationProxyService

from sparserestore import backup, perform_restore


def exit(code=0):
    if platform.system() == "Windows" and getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        input("Press Enter to exit...")

    sys.exit(code)


@click.command(cls=Command)
@click.option('--helper-file', '-f', type=click.Path(exists=True, readable=True, dir_okay=False, path_type=Path),
              help='Path to the TrollStore Helper file (PersistenceHelper_Embedded)')
@click.pass_context
def cli(ctx, service_provider: LockdownClient, helper_file: Path = None) -> None:
    os_names = {
        "iPhone": "iOS",
        "iPad": "iPadOS",
        "iPod": "iOS",
        "AppleTV": "tvOS",
        "Watch": "watchOS",
        "AudioAccessory": "HomePod Software Version",
        "RealityDevice": "visionOS",
    }

    device_class = service_provider.get_value(key="DeviceClass")
    device_build = service_provider.get_value(key="BuildVersion")
    device_version = parse_version(service_provider.product_version)

    if not all([device_class, device_build, device_version]):
        click.secho("Failed to get device information!", fg="red")
        click.secho("Make sure your device is connected and try again.", fg="red")
        return

    os_name = (os_names[device_class] + " ") if device_class in os_names else ""
    if (
        device_version < parse_version("15.0")
        or device_version > parse_version("17.0")
        or parse_version("16.7") < device_version < parse_version("17.0")
        or device_version == parse_version("16.7")
        and device_build != "20H18"  # 16.7 RC
    ):
        click.secho(f"{os_name}{device_version} ({device_build}) is not supported.", fg="red")
        click.secho("This tool is only compatible with iOS/iPadOS 15.0 - 16.7 RC and 17.0.", fg="red")
        return

    # 获取TrollStore Helper文件内容
    helper_contents = None
    if helper_file:
        try:
            helper_contents = helper_file.read_bytes()
            click.secho(f"Loaded TrollStore Helper from: {helper_file}", fg="green")
        except Exception as e:
            click.secho(f"Failed to read helper file: {e}", fg="red")
            return
    else:
        # 如果没有通过命令行参数指定，则交互式询问
        click.secho("\nPlease provide the TrollStore Helper file (PersistenceHelper_Embedded)", fg="yellow")
        helper_path_str = click.prompt("Enter the path to the helper file", type=str)
        helper_path = Path(helper_path_str).expanduser().resolve()
        
        if not helper_path.exists():
            click.secho(f"File not found: {helper_path}", fg="red")
            return
        if not helper_path.is_file():
            click.secho(f"Not a file: {helper_path}", fg="red")
            return
            
        try:
            helper_contents = helper_path.read_bytes()
            click.secho(f"Loaded TrollStore Helper from: {helper_path}", fg="green")
        except Exception as e:
            click.secho(f"Failed to read helper file: {e}", fg="red")
            return

    app = click.prompt(
        """
Please specify the removable system app you want to replace with TrollStore Helper.
If you don't know which app to specify, specify the Tips app.

Enter the app name"""
    )

    if not app.endswith(".app"):
        app += ".app"

    apps_json = InstallationProxyService(service_provider).get_apps(application_type="System", calculate_sizes=False)

    app_path = None
    for key, value in apps_json.items():
        if isinstance(value, dict) and "Path" in value:
            potential_path = Path(value["Path"])
            if potential_path.name.lower() == app.lower():
                app_path = potential_path
                app = app_path.name

    if not app_path:
        click.secho(f"Failed to find the removable system app '{app}'!", fg="red")
        click.secho(f"Make sure you typed the app name correctly, and that the system app '{app}' is installed to your device.", fg="red")
        return
    elif Path("/private/var/containers/Bundle/Application") not in app_path.parents:
        click.secho(f"'{app}' is not a removable system app!", fg="red")
        click.secho("Please choose a removable system app. These will be Apple-made apps that can be deleted and re-downloaded.", fg="red")
        return

    app_uuid = app_path.parent.name

    click.secho(f"Replacing {app} with TrollStore Helper. (UUID: {app_uuid})", fg="yellow")

    back = backup.Backup(
        files=[
            backup.Directory("", "RootDomain"),
            backup.Directory("Library", "RootDomain"),
            backup.Directory("Library/Preferences", "RootDomain"),
            backup.ConcreteFile("Library/Preferences/temp", "RootDomain", owner=33, group=33, contents=helper_contents, inode=0),
            backup.Directory(
                "",
                f"SysContainerDomain-../../../../../../../../var/backup/var/containers/Bundle/Application/{app_uuid}/{app}",
                owner=33,
                group=33,
            ),
            backup.ConcreteFile(
                "",
                f"SysContainerDomain-../../../../../../../../var/backup/var/containers/Bundle/Application/{app_uuid}/{app}/{app.split('.')[0]}",
                owner=33,
                group=33,
                contents=b"",
                inode=0,
            ),
            backup.ConcreteFile(
                "",
                "SysContainerDomain-../../../../../../../../var/.backup.i/var/root/Library/Preferences/temp",
                owner=501,
                group=501,
                contents=b"",
            ),  # Break the hard link
            backup.ConcreteFile("", "SysContainerDomain-../../../../../../../.." + "/crash_on_purpose", contents=b""),
        ]
    )

    try:
        perform_restore(back, reboot=False)
    except PyMobileDevice3Exception as e:
        if "Find My" in str(e):
            click.secho("Find My must be disabled in order to use this tool.", fg="red")
            click.secho("Disable Find My from Settings (Settings -> [Your Name] -> Find My) and then try again.", fg="red")
            exit(1)
        elif "crash_on_purpose" not in str(e):
            raise e

    click.secho("Rebooting device", fg="green")

    with DiagnosticsService(service_provider) as diagnostics_service:
        diagnostics_service.restart()

    click.secho("Make sure you turn Find My iPhone back on if you use it after rebooting.", fg="green")
    click.secho("Make sure to install a proper persistence helper into the app you chose after installing TrollStore!\n", fg="green")


def main():
    try:
        cli(standalone_mode=False)
    except NoDeviceConnectedError:
        click.secho("No device connected!", fg="red")
        click.secho("Please connect your device and try again.", fg="red")
        exit(1)
    except click.UsageError as e:
        click.secho(e.format_message(), fg="red")
        click.echo(cli.get_help(click.Context(cli)))
        exit(2)
    except Exception:
        click.secho("An error occurred!", fg="red")
        click.secho(traceback.format_exc(), fg="red")
        exit(1)

    exit(0)


if __name__ == "__main__":
    main()
