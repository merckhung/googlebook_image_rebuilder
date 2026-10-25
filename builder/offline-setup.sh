#!/system/bin/sh
# Opt-in local setup for a Googlebook image built with GBOS_SKIP_SETUP=1.
# Runs after Android boot, including when updating an installation stuck in SUW.
# Also usable over an already-authorized adb shell: adb shell sh < this-file.
set -eu

say() {
    echo "gbos-offline-setup: $*"
    log -t GBOSOfflineSetup "$*"
    if [ -w /dev/kmsg ]; then
        printf '<6>gbos-offline-setup: %s\n' "$*" > /dev/kmsg
    fi
}
trap 'status=$?; if [ "$status" -ne 0 ]; then say "failed with status $status"; fi' EXIT

# Android Desktop has a headless system user and a separate main user. Discover
# that user instead of assuming user 10. Android versions print either an ID or
# "Main user id: ID" for this command.
main_user=$(timeout 20 cmd user get-main-user | sed -n 's/^Main user id: //; /^[0-9][0-9]*$/p')
case "$main_user" in
    ''|*[!0-9]*)
        # Lapis can report "None": user 0 is headless and the one full user
        # has not been assigned Android's MAIN flag. Refuse an ambiguous list.
        main_user=$(timeout 20 cmd user list -v | sed -n '/type=full\./s/^[^:]*: id=\([0-9][0-9]*\),.*/\1/p')
        ;;
esac
case "$main_user" in
    ''|*[!0-9]*) say "could not determine the main user; setup unchanged"; exit 1 ;;
esac
complete=$(timeout 20 settings --user "$main_user" get secure user_setup_complete)
disabled=$(timeout 20 pm list packages -d --user "$main_user" com.google.android.desktop.setupwizard)
if [ "$complete" = 1 ] && echo "$disabled" | grep -qx 'package:com.google.android.desktop.setupwizard'; then
    say "user $main_user already completed setup; leaving the login screen unchanged"
    exit 0
fi

say "completing offline setup for user $main_user"

# Only the setup applications are disabled. Wi-Fi and the normal Settings and
# update services remain available after setup.
for package in com.google.android.desktop.setupwizard com.google.android.setupwizard; do
    installed=$(timeout 20 pm list packages --user "$main_user" "$package")
    if echo "$installed" | grep -qx "package:$package"; then
        timeout 20 pm disable-user --user "$main_user" "$package"
    fi
done
for user_id in 0 "$main_user"; do
    timeout 20 settings --user "$user_id" put secure user_setup_complete 1
done
timeout 20 settings put global device_provisioned 1
if [ "$(timeout 20 am get-current-user)" != "$main_user" ]; then
    timeout 30 am switch-user "$main_user"
fi
timeout 30 am start --user "$main_user" -a android.intent.action.MAIN -c android.intent.category.HOME
say "offline setup complete; Wi-Fi can be configured in Settings"
