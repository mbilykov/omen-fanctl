// SPDX-License-Identifier: GPL-2.0-only
/*
 * Read-only probe for the four temperature inputs used by HP Gaming Hub.
 *
 * Gaming Hub calls BiosWmiCmd_GetSync(0x20008, 0x23, { index, 0, 0, 0 },
 * 4, 4).  This module performs the equivalent WMI method and exposes the
 * result through /proc/hp_wmi_sensors.  It never changes fan or power state.
 */

#include <linux/acpi.h>
#include <linux/errno.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/wmi.h>

#define HP_WMI_BIOS_GUID "5FB7F034-2C63-45E9-BE91-3D44E2C707E4"
#define HP_WMI_SIGNATURE 0x55434553
#define HP_WMI_SENSOR_COMMAND 0x20008
#define HP_WMI_SENSOR_QUERY 0x23
#define HP_WMI_READ_METHOD_ID 2
#define HP_WMI_DATA_SIZE 128

struct hp_wmi_bios_args {
	u32 signature;
	u32 command;
	u32 command_type;
	u32 data_size;
	u8 data[HP_WMI_DATA_SIZE];
};

struct hp_wmi_bios_return {
	u32 signature;
	u32 return_code;
	u8 data[HP_WMI_DATA_SIZE];
};

static struct proc_dir_entry *sensor_proc;

static int read_sensor(u8 index, u8 *temperature)
{
	struct hp_wmi_bios_args *args;
	struct hp_wmi_bios_return *response;
	struct acpi_buffer input;
	struct acpi_buffer output = { ACPI_ALLOCATE_BUFFER, NULL };
	union acpi_object *obj;
	acpi_status status;
	int ret = 0;

	args = kzalloc(sizeof(*args), GFP_KERNEL);
	if (!args)
		return -ENOMEM;

	args->signature = HP_WMI_SIGNATURE;
	args->command = HP_WMI_SENSOR_COMMAND;
	args->command_type = HP_WMI_SENSOR_QUERY;
	args->data_size = 4;
	args->data[0] = index;

	input.length = sizeof(*args);
	input.pointer = args;
	status = wmi_evaluate_method(HP_WMI_BIOS_GUID, 0,
				     HP_WMI_READ_METHOD_ID, &input, &output);
	if (ACPI_FAILURE(status)) {
		ret = -EIO;
		goto out_args;
	}

	obj = output.pointer;
	if (!obj || obj->type != ACPI_TYPE_BUFFER ||
	    obj->buffer.length < offsetof(struct hp_wmi_bios_return, data) + 1) {
		ret = -EPROTO;
		goto out_output;
	}

	response = (struct hp_wmi_bios_return *)obj->buffer.pointer;
	if (response->return_code) {
		ret = -EREMOTEIO;
		goto out_output;
	}

	*temperature = response->data[0];

out_output:
	kfree(output.pointer);
out_args:
	kfree(args);
	return ret;
}

static int sensors_show(struct seq_file *m, void *unused)
{
	static const char * const names[] = { "IR", "Ambient", "PCH", "VR" };
	int i;

	seq_puts(m, "index name temp_c\n");
	for (i = 0; i < ARRAY_SIZE(names); i++) {
		u8 temperature = 0;
		int ret = read_sensor(i, &temperature);

		if (ret)
			seq_printf(m, "%d %s error:%d\n", i, names[i], ret);
		else
			seq_printf(m, "%d %s %u\n", i, names[i], temperature);
	}

	return 0;
}

static int sensors_open(struct inode *inode, struct file *file)
{
	return single_open(file, sensors_show, NULL);
}

static const struct proc_ops sensors_proc_ops = {
	.proc_open = sensors_open,
	.proc_read = seq_read,
	.proc_lseek = seq_lseek,
	.proc_release = single_release,
};

static int __init hp_wmi_sensor_probe_init(void)
{
	if (!wmi_has_guid(HP_WMI_BIOS_GUID))
		return -ENODEV;

	sensor_proc = proc_create("hp_wmi_sensors", 0444, NULL,
				  &sensors_proc_ops);
	if (!sensor_proc)
		return -ENOMEM;

	pr_info("hp_wmi_sensor_probe: read-only sensor probe loaded\n");
	return 0;
}

static void __exit hp_wmi_sensor_probe_exit(void)
{
	proc_remove(sensor_proc);
	pr_info("hp_wmi_sensor_probe: unloaded\n");
}

module_init(hp_wmi_sensor_probe_init);
module_exit(hp_wmi_sensor_probe_exit);

MODULE_AUTHOR("fan-control-daemon-research");
MODULE_DESCRIPTION("Read-only HP WMI temperature sensor probe");
MODULE_LICENSE("GPL");
