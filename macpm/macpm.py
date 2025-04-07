import argparse
import humanize
from collections import deque
from dashing import VSplit, HSplit, HGauge, HChart, VGauge, HBrailleChart, HBrailleFilledChart
import os, time
import subprocess
from subprocess import PIPE
import psutil
import plistlib
import curses

version = 'macpm v0.24'
parser = argparse.ArgumentParser(
    description=f'{version}: Performance monitoring CLI tool for Apple Silicon')
parser.add_argument('--interval', type=int, default=1,
                    help='Display interval and sampling interval for powermetrics (seconds)')
parser.add_argument('--color', type=int, default=2,
                    help='Choose display color (0~8)')
parser.add_argument('--avg', type=int, default=30,
                    help='Interval for averaged values (seconds)')
parser.add_argument('--show_cores', type=bool, default=False,
                    help='Choose show cores mode')

args = parser.parse_args()

powermetrics_process = None

def clear_console():
    command = 'clear'
    os.system(command)


def convert_to_GB(value):
    return round(value/1024/1024/1024, 1)


def get_ram_metrics_dict():
    ram_metrics = psutil.virtual_memory()
    swap_metrics = psutil.swap_memory()
    total_GB = convert_to_GB(ram_metrics.total)
    free_GB = convert_to_GB(ram_metrics.available)
    used_GB = convert_to_GB(ram_metrics.total-ram_metrics.available)
    swap_total_GB = convert_to_GB(swap_metrics.total)
    swap_used_GB = convert_to_GB(swap_metrics.used)
    swap_free_GB = convert_to_GB(swap_metrics.total-swap_metrics.used)
    if swap_total_GB > 0:
        swap_free_percent = int(100-(swap_free_GB/swap_total_GB*100))
    else:
        swap_free_percent = None
    ram_metrics_dict = {
        "total_GB": round(total_GB, 1),
        "free_GB": round(free_GB, 1),
        "used_GB": round(used_GB, 1),
        "free_percent": int(100-(ram_metrics.available/ram_metrics.total*100)),
        "swap_total_GB": swap_total_GB,
        "swap_used_GB": swap_used_GB,
        "swap_free_GB": swap_free_GB,
        "swap_free_percent": swap_free_percent,
    }
    return ram_metrics_dict


def get_cpu_info():
    cpu_info = os.popen('sysctl -a | grep machdep.cpu').read()
    cpu_info_lines = cpu_info.split("\n")
    data_fields = ["machdep.cpu.brand_string", "machdep.cpu.core_count"]
    cpu_info_dict = {}
    for l in cpu_info_lines:
        for h in data_fields:
            if h in l:
                value = l.split(":")[1].strip()
                cpu_info_dict[h] = value
    return cpu_info_dict


def get_core_counts():
    cores_info = os.popen('sysctl -a | grep hw.perflevel').read()
    cores_info_lines = cores_info.split("\n")
    data_fields = ["hw.perflevel0.logicalcpu", "hw.perflevel1.logicalcpu"]
    cores_info_dict = {}
    for l in cores_info_lines:
        for h in data_fields:
            if h in l:
                value = int(l.split(":")[1].strip())
                cores_info_dict[h] = value
    return cores_info_dict


def get_gpu_cores():
    try:
        cores = os.popen(
            "system_profiler -detailLevel basic SPDisplaysDataType | grep 'Total Number of Cores'").read()
        cores = int(cores.split(": ")[-1])
    except:
        cores = "?"
    return cores


def get_soc_info():
    cpu_info_dict = get_cpu_info()
    core_counts_dict = get_core_counts()
    try:
        e_core_count = core_counts_dict["hw.perflevel1.logicalcpu"]
        p_core_count = core_counts_dict["hw.perflevel0.logicalcpu"]
    except:
        e_core_count = "?"
        p_core_count = "?"
    soc_info = {
        "name": cpu_info_dict["machdep.cpu.brand_string"],
        "core_count": int(cpu_info_dict["machdep.cpu.core_count"]),
        "cpu_max_power": None,
        "gpu_max_power": None,
        "cpu_max_bw": None,
        "gpu_max_bw": None,
        "e_core_count": e_core_count,
        "p_core_count": p_core_count,
        "gpu_core_count": get_gpu_cores()
    }
    # TDP (power)
    if soc_info["name"] == "Apple M1 Max":
        soc_info["cpu_max_power"] = 30
        soc_info["gpu_max_power"] = 60
    elif soc_info["name"] == "Apple M1 Pro":
        soc_info["cpu_max_power"] = 30
        soc_info["gpu_max_power"] = 30
    elif soc_info["name"] == "Apple M1":
        soc_info["cpu_max_power"] = 20
        soc_info["gpu_max_power"] = 20
    elif soc_info["name"] == "Apple M1 Ultra":
        soc_info["cpu_max_power"] = 60
        soc_info["gpu_max_power"] = 120
    elif soc_info["name"] == "Apple M2":
        soc_info["cpu_max_power"] = 25
        soc_info["gpu_max_power"] = 15
    else:
        soc_info["cpu_max_power"] = 20
        soc_info["gpu_max_power"] = 20
    # bandwidth
    if soc_info["name"] == "Apple M1 Max":
        soc_info["cpu_max_bw"] = 250
        soc_info["gpu_max_bw"] = 400
    elif soc_info["name"] == "Apple M1 Pro":
        soc_info["cpu_max_bw"] = 200
        soc_info["gpu_max_bw"] = 200
    elif soc_info["name"] == "Apple M1":
        soc_info["cpu_max_bw"] = 70
        soc_info["gpu_max_bw"] = 70
    elif soc_info["name"] == "Apple M1 Ultra":
        soc_info["cpu_max_bw"] = 500
        soc_info["gpu_max_bw"] = 800
    elif soc_info["name"] == "Apple M2":
        soc_info["cpu_max_bw"] = 100
        soc_info["gpu_max_bw"] = 100
    else:
        soc_info["cpu_max_bw"] = 70
        soc_info["gpu_max_bw"] = 70
    return soc_info

def parse_thermal_pressure(powermetrics_parse):
    return powermetrics_parse["thermal_pressure"]


def parse_bandwidth_metrics(powermetrics_parse):
    bandwidth_metrics = powermetrics_parse["bandwidth_counters"]
    bandwidth_metrics_dict = {}
    data_fields = ["PCPU0 DCS RD", "PCPU0 DCS WR",
                   "PCPU1 DCS RD", "PCPU1 DCS WR",
                   "PCPU2 DCS RD", "PCPU2 DCS WR",
                   "PCPU3 DCS RD", "PCPU3 DCS WR",
                   "PCPU DCS RD", "PCPU DCS WR",
                   "ECPU0 DCS RD", "ECPU0 DCS WR",
                   "ECPU1 DCS RD", "ECPU1 DCS WR",
                   "ECPU DCS RD", "ECPU DCS WR",
                   "GFX DCS RD", "GFX DCS WR",
                   "ISP DCS RD", "ISP DCS WR",
                   "STRM CODEC DCS RD", "STRM CODEC DCS WR",
                   "PRORES DCS RD", "PRORES DCS WR",
                   "VDEC DCS RD", "VDEC DCS WR",
                   "VENC0 DCS RD", "VENC0 DCS WR",
                   "VENC1 DCS RD", "VENC1 DCS WR",
                   "VENC2 DCS RD", "VENC2 DCS WR",
                   "VENC3 DCS RD", "VENC3 DCS WR",
                   "VENC DCS RD", "VENC DCS WR",
                   "JPG0 DCS RD", "JPG0 DCS WR",
                   "JPG1 DCS RD", "JPG1 DCS WR",
                   "JPG2 DCS RD", "JPG2 DCS WR",
                   "JPG3 DCS RD", "JPG3 DCS WR",
                   "JPG DCS RD", "JPG DCS WR",
                   "DCS RD", "DCS WR"]
    for h in data_fields:
        bandwidth_metrics_dict[h] = 0
    for l in bandwidth_metrics:
        if l["name"] in data_fields:
            bandwidth_metrics_dict[l["name"]] = l["value"]/(1e9)
    bandwidth_metrics_dict["PCPU DCS RD"] = bandwidth_metrics_dict["PCPU DCS RD"] + \
        bandwidth_metrics_dict["PCPU0 DCS RD"] + \
        bandwidth_metrics_dict["PCPU1 DCS RD"] + \
        bandwidth_metrics_dict["PCPU2 DCS RD"] + \
        bandwidth_metrics_dict["PCPU3 DCS RD"]
    bandwidth_metrics_dict["PCPU DCS WR"] = bandwidth_metrics_dict["PCPU DCS WR"] + \
        bandwidth_metrics_dict["PCPU0 DCS WR"] + \
        bandwidth_metrics_dict["PCPU1 DCS WR"] + \
        bandwidth_metrics_dict["PCPU2 DCS WR"] + \
        bandwidth_metrics_dict["PCPU3 DCS WR"]
    bandwidth_metrics_dict["JPG DCS RD"] = bandwidth_metrics_dict["JPG DCS RD"] + \
        bandwidth_metrics_dict["JPG0 DCS RD"] + \
        bandwidth_metrics_dict["JPG1 DCS RD"] + \
        bandwidth_metrics_dict["JPG2 DCS RD"] + \
        bandwidth_metrics_dict["JPG3 DCS RD"]
    bandwidth_metrics_dict["JPG DCS WR"] = bandwidth_metrics_dict["JPG DCS WR"] + \
        bandwidth_metrics_dict["JPG0 DCS WR"] + \
        bandwidth_metrics_dict["JPG1 DCS WR"] + \
        bandwidth_metrics_dict["JPG2 DCS WR"] + \
        bandwidth_metrics_dict["JPG3 DCS WR"]
    bandwidth_metrics_dict["VENC DCS RD"] = bandwidth_metrics_dict["VENC DCS RD"] + \
        bandwidth_metrics_dict["VENC0 DCS RD"] + \
        bandwidth_metrics_dict["VENC1 DCS RD"] + \
        bandwidth_metrics_dict["VENC2 DCS RD"] + \
        bandwidth_metrics_dict["VENC3 DCS RD"]
    bandwidth_metrics_dict["VENC DCS WR"] = bandwidth_metrics_dict["VENC DCS WR"] + \
        bandwidth_metrics_dict["VENC0 DCS WR"] + \
        bandwidth_metrics_dict["VENC1 DCS WR"] + \
        bandwidth_metrics_dict["VENC2 DCS WR"] + \
        bandwidth_metrics_dict["VENC3 DCS WR"]
    bandwidth_metrics_dict["MEDIA DCS"] = sum([
        bandwidth_metrics_dict["ISP DCS RD"], bandwidth_metrics_dict["ISP DCS WR"],
        bandwidth_metrics_dict["STRM CODEC DCS RD"], bandwidth_metrics_dict["STRM CODEC DCS WR"],
        bandwidth_metrics_dict["PRORES DCS RD"], bandwidth_metrics_dict["PRORES DCS WR"],
        bandwidth_metrics_dict["VDEC DCS RD"], bandwidth_metrics_dict["VDEC DCS WR"],
        bandwidth_metrics_dict["VENC DCS RD"], bandwidth_metrics_dict["VENC DCS WR"],
        bandwidth_metrics_dict["JPG DCS RD"], bandwidth_metrics_dict["JPG DCS WR"],
    ])
    return bandwidth_metrics_dict


def parse_cpu_metrics(powermetrics_parse):
    e_core = []
    p_core = []
    cpu_metrics = powermetrics_parse["processor"]
    cpu_metric_dict = {}
    # cpu_clusters
    cpu_clusters = cpu_metrics["clusters"]
    e_total_idle_ratio = 0
    e_core_count = 0
    p_total_idle_ratio = 0
    p_core_count = 0
    for cluster in cpu_clusters:
        name = cluster["name"]
        cpu_metric_dict[name+"_freq_Mhz"] = int(cluster["freq_hz"]/(1e6))
        cpu_metric_dict[name+"_active"] = int((1 - cluster["idle_ratio"])*100)
        for cpu in cluster["cpus"]:
            name = 'E-Cluster' if name[0] == 'E' else 'P-Cluster'
            core = e_core if name[0] == 'E' else p_core
            core.append(cpu["cpu"])
            cpu_metric_dict[name + str(cpu["cpu"]) + "_freq_Mhz"] = int(cpu["freq_hz"] / (1e6))
            #there  is no down_ratio in M1
            if cluster.get("down_ratio") is None:
                idle_ratio = cpu["idle_ratio"]
            else:
                idle_ratio = cluster["down_ratio"] + (1 - cluster["down_ratio"]) * (cpu["idle_ratio"] + cpu["down_ratio"])
            cpu_metric_dict[name + str(cpu["cpu"]) + "_active"] = int((1 - idle_ratio) * 100)           
            if name[0] == 'E':
                e_total_idle_ratio += idle_ratio
                e_core_count += 1
            else:
                p_total_idle_ratio += idle_ratio
                p_core_count += 1
    
    cpu_metric_dict["E-Cluster_active"] = int((1 - e_total_idle_ratio/e_core_count)*100)
    cpu_metric_dict["P-Cluster_active"] = int((1 - p_total_idle_ratio/p_core_count)*100)
    cpu_metric_dict["e_core"] = e_core
    cpu_metric_dict["p_core"] = p_core
    if "E-Cluster_active" not in cpu_metric_dict:
        # M1 Ultra
        cpu_metric_dict["E-Cluster_active"] = int(
            (cpu_metric_dict["E0-Cluster_active"] + cpu_metric_dict["E1-Cluster_active"])/2)
    if "E-Cluster_freq_Mhz" not in cpu_metric_dict:
        # M1 Ultra
        cpu_metric_dict["E-Cluster_freq_Mhz"] = max(
            cpu_metric_dict["E0-Cluster_freq_Mhz"], cpu_metric_dict["E1-Cluster_freq_Mhz"])
    if "P-Cluster_active" not in cpu_metric_dict:
        if "P2-Cluster_active" in cpu_metric_dict:
            # M1 Ultra
            cpu_metric_dict["P-Cluster_active"] = int((cpu_metric_dict["P0-Cluster_active"] + cpu_metric_dict["P1-Cluster_active"] +
                                                      cpu_metric_dict["P2-Cluster_active"] + cpu_metric_dict["P3-Cluster_active"]) / 4)
        else:
            cpu_metric_dict["P-Cluster_active"] = int(
                (cpu_metric_dict["P0-Cluster_active"] + cpu_metric_dict["P1-Cluster_active"])/2)
    if "P-Cluster_freq_Mhz" not in cpu_metric_dict:
        if "P2-Cluster_freq_Mhz" in cpu_metric_dict:
            # M1 Ultra
            freqs = [
                cpu_metric_dict["P0-Cluster_freq_Mhz"],
                cpu_metric_dict["P1-Cluster_freq_Mhz"],
                cpu_metric_dict["P2-Cluster_freq_Mhz"],
                cpu_metric_dict["P3-Cluster_freq_Mhz"]]
            cpu_metric_dict["P-Cluster_freq_Mhz"] = max(freqs)
        else:
            cpu_metric_dict["P-Cluster_freq_Mhz"] = max(
                cpu_metric_dict["P0-Cluster_freq_Mhz"], cpu_metric_dict["P1-Cluster_freq_Mhz"])
    # power
    cpu_metric_dict["ane_W"] = cpu_metrics["ane_energy"]/1000
    #cpu_metric_dict["dram_W"] = cpu_metrics["dram_energy"]/1000
    cpu_metric_dict["cpu_W"] = cpu_metrics["cpu_energy"]/1000
    cpu_metric_dict["gpu_W"] = cpu_metrics["gpu_energy"]/1000
    cpu_metric_dict["package_W"] = cpu_metrics["combined_power"]/1000
    return cpu_metric_dict


def parse_gpu_metrics(powermetrics_parse):
    gpu_metrics = powermetrics_parse["gpu"]
    gpu_metrics_dict = {
        "freq_MHz": int(gpu_metrics["freq_hz"]),
        "active": int((1 - gpu_metrics["idle_ratio"])*100),
    }
    return gpu_metrics_dict

def parse_disk_metrics(powermetrics_parse):
    disk_metrics = powermetrics_parse.get("disk",{})
    disk_metrics_dict = {
        "read_iops": int(disk_metrics.get("rops_per_s",0)),
        "write_iops": int(disk_metrics.get("wops_per_s",0)),
        "read_Bps": int(disk_metrics.get("rbytes_per_s",0)),
        "write_Bps": int(disk_metrics.get("wbytes_per_s",0)),
    }
    return disk_metrics_dict

def parse_network_metrics(powermetrics_parse):
    network_metrics = powermetrics_parse.get("network",{})
    network_metrics_dict = {
        "out_Bps": int(network_metrics.get("obyte_rate",0)),
        "in_Bps": int(network_metrics.get("ibyte_rate",0)),
    }
    return network_metrics_dict


class DefaultView():
    def __init__(self,soc_info_dict,args):
        self.cpu_peak_power = 0
        self.gpu_peak_power = 0
        self.package_peak_power = 0
        self.disk_read_iops_peak = 0
        self.disk_write_iops_peak = 0
        self.disk_read_bps_peak = 0
        self.disk_write_bps_peak = 0
        self.network_in_bps_peak = 0
        self.network_out_bps_peak = 0
        self.default_cpu_perline = 8
        self.construct(soc_info_dict,args)
        
    def construct(self,soc_info_dict,args):
        self.cpu1_gauge = HGauge(title="E-CPU Usage", val=0, color=args.color)
        self.cpu2_gauge = HGauge(title="P-CPU Usage", val=0, color=args.color)
        self.gpu_gauge = HGauge(title="GPU Usage", val=0, color=args.color)
        self.ane_gauge = HGauge(title="ANE", val=0, color=args.color)
        self.gpu_ane_gauges = [self.gpu_gauge, self.ane_gauge]
        self.e_core_count = soc_info_dict["e_core_count"]
        self.e_core_gauges = [VGauge(val=0, color=args.color, border_color=args.color) for _ in range(self.e_core_count)]
        self.p_core_count = soc_info_dict["p_core_count"]
        self.max_cpu_perline = self.default_cpu_perline
        for i in range(int(self.default_cpu_perline/2),self.default_cpu_perline):
            if self.p_core_count % i == 0:
                self.max_cpu_perline = i
                break
        import math
        p_core_lines = math.ceil(self.p_core_count / self.max_cpu_perline)
        self.p_core_gauges = []
        self.p_core_split = []
        for i in range(p_core_lines):
            self.p_core_gauges.append([])
            self.p_core_gauges[i].extend([VGauge(val=0, color=args.color, border_color=args.color) for _ in range(self.max_cpu_perline if i < p_core_lines - 1 else self.p_core_count - i * self.max_cpu_perline)])
            self.p_core_split.append(HSplit(
                *self.p_core_gauges[i],
            ))
        if args.show_cores:
            self.processor_gauges = [self.cpu1_gauge,
                            HSplit(*self.e_core_gauges),
                            self.cpu2_gauge,]
            #for i in range(len(self.p_core_split)):
            self.processor_gauges.extend(self.p_core_split)
            self.processor_gauges.extend(self.gpu_ane_gauges)
        else:
            self.processor_gauges = [
                HSplit(self.cpu1_gauge, self.cpu2_gauge),
                HSplit(*self.gpu_ane_gauges)
            ]
        """
        self.processor_gauges = [self.cpu1_gauge,
                            HSplit(*self.e_core_gauges),
                            self.cpu2_gauge,
                            *self.p_core_split,
                            *self.gpu_ane_gauges
                            ] if args.show_cores else [
            HSplit(self.cpu1_gauge, self.cpu2_gauge),
            HSplit(*self.gpu_ane_gauges)
        ]
        """
        self.processor_split = VSplit(
            *self.processor_gauges,
            title="Processor Utilization",
            border_color=args.color,
        )

        self.ram_gauge = HGauge(title="RAM Usage", val=0, color=args.color)
        """
        ecpu_bw_gauge = HGauge(title="E-CPU B/W", val=50, color=args.color)
        pcpu_bw_gauge = HGauge(title="P-CPU B/W", val=50, color=args.color)
        gpu_bw_gauge = HGauge(title="GPU B/W", val=50, color=args.color)
        media_bw_gauge = HGauge(title="Media B/W", val=50, color=args.color)
        bw_gauges = [HSplit(
            ecpu_bw_gauge,
            pcpu_bw_gauge,
        ),
            HSplit(
                gpu_bw_gauge,
                media_bw_gauge,
            )] if args.show_cores else [
            HSplit(
                ecpu_bw_gauge,
                pcpu_bw_gauge,
                gpu_bw_gauge,
                media_bw_gauge,
            )]
        """
        self.memory_gauges = VSplit(
            self.ram_gauge,
            #*bw_gauges,
            border_color=args.color,
            title="Memory"
        )

        self.cpu_power_chart = HChart(title="CPU Power", color=args.color)
        self.gpu_power_chart = HChart(title="GPU Power", color=args.color)
        self.power_charts = VSplit(
            self.cpu_power_chart,
            self.gpu_power_chart,
            title="Power Chart",
            border_color=args.color,
        ) if args.show_cores else HSplit(
            self.cpu_power_chart,
            self.gpu_power_chart,
            title="Power Chart",
            border_color=args.color,
        )

        self.disk_read_iops_charts = HChart(title="read iops", color=args.color)
        self.disk_write_iops_charts = HChart(title="write iops", color=args.color)
        self.disk_read_bps_charts = HChart(title="read Bps", color=args.color)
        self.disk_write_bps_charts = HChart(title="write Bps", color=args.color)
        self.network_in_bps_charts = HChart(title="in Bps", color=args.color)
        self.network_out_bps_charts = HChart(title="out Bps", color=args.color)
        self.disk_io_charts = HSplit(
            VSplit(self.disk_read_iops_charts,
            self.disk_write_iops_charts,),
            VSplit(self.disk_read_bps_charts,
            self.disk_write_bps_charts,),
            title="Disk IO", 
            color=args.color,
            border_color=args.color)
        
        self.network_io_charts = HSplit(
            self.network_in_bps_charts,
            self.network_out_bps_charts,
            title="Network IO", 
            color=args.color,
            border_color=args.color)
        
        self.ui = HSplit(
            self.processor_split,
            VSplit(
                self.memory_gauges,
                self.power_charts,
                self.disk_io_charts,
                self.network_io_charts,
            )
        ) if args.show_cores else VSplit(
            self.processor_split,
            self.memory_gauges,
            self.power_charts,
            self.disk_io_charts,
            self.network_io_charts,
        )
        """
        ui.title = "".join([
            version,
            "  (Press q or ESC to stop)"
        ])
        ui.border_color = args.color
        """
        self.usage_gauges = self.ui.items[0]
        #bw_gauges = memory_gauges.items[1]

        cpu_title = "".join([
            soc_info_dict["name"],
            " (cores: ",
            str(soc_info_dict["e_core_count"]),
            "E+",
            str(soc_info_dict["p_core_count"]),
            "P+",
            str(soc_info_dict["gpu_core_count"]),
            "GPU)"
        ])
        self.usage_gauges.title = cpu_title
        self.cpu_max_power = soc_info_dict["cpu_max_power"]
        self.gpu_max_power = soc_info_dict["gpu_max_power"]
        self.ane_max_power = 16.0
        """max_cpu_bw = soc_info_dict["cpu_max_bw"]
        max_gpu_bw = soc_info_dict["gpu_max_bw"]
        max_media_bw = 7.0"""

        self.avg_package_power_list = deque([], maxlen=int(args.avg / args.interval))
        self.avg_cpu_power_list = deque([], maxlen=int(args.avg / args.interval))
        self.avg_gpu_power_list = deque([], maxlen=int(args.avg / args.interval))

    def display(self,powermetrics_parse,args):
        if args.color != self.gpu_gauge.color:
            clear_console()
            self.gpu_gauge.color = args.color
            self.ane_gauge.color = args.color
            self.cpu1_gauge.color = args.color
            self.cpu2_gauge.color = args.color
            self.power_charts.color = args.color
            self.power_charts.border_color = args.color
            self.processor_split.border_color = args.color
            self.ram_gauge.color = args.color
            self.memory_gauges.border_color = args.color
            self.cpu_power_chart.color = args.color
            self.gpu_power_chart.color = args.color
            #self.cpu_power_chart.border_color = args.color
            self.disk_io_charts.color = args.color
            self.disk_io_charts.border_color = args.color
            self.network_io_charts.color = args.color
            self.network_io_charts.border_color = args.color
            self.disk_read_iops_charts.color = args.color
            self.disk_write_iops_charts.color = args.color
            self.disk_read_bps_charts.color = args.color
            self.disk_write_bps_charts.color = args.color
            self.network_in_bps_charts.color = args.color
            self.network_out_bps_charts.color = args.color
            for i in range(len(self.e_core_gauges)):
                self.e_core_gauges[i].color = args.color
                self.e_core_gauges[i].border_color = args.color
            for i in range(len(self.p_core_gauges)):
                for j in range(len(self.p_core_gauges[i])):
                    self.p_core_gauges[i][j].color = args.color
                    self.p_core_gauges[i][j].border_color = args.color
            """
            for i in range(len(self.p_core_gauges_ext)):
                self.p_core_gauges_ext[i].color = args.color
                self.p_core_gauges_ext[i].border_color = args.color
            """
        thermal_pressure = parse_thermal_pressure(powermetrics_parse)
        cpu_metrics_dict = parse_cpu_metrics(powermetrics_parse)
        gpu_metrics_dict = parse_gpu_metrics(powermetrics_parse)
        disk_metrics_dict = parse_disk_metrics(powermetrics_parse)
        network_metrics_dict = parse_network_metrics(powermetrics_parse)
        #bandwidth_metrics = parse_bandwidth_metrics(powermetrics_parse)
        bandwidth_metrics = None
        timestamp = powermetrics_parse["timestamp"]
        if timestamp :
            if thermal_pressure == "Nominal":
                thermal_throttle = "no"
            else:
                thermal_throttle = "yes"

            """e_cpu_usage = 0
            core_count = 0
            for i in cpu_metrics_dict["e_core"]:
                e_cpu_usage += cpu_metrics_dict["E-Cluster" + str(i) + "_active"]
                core_count += 1
            e_cpu_usage = (e_cpu_usage / core_count) if core_count > 0 else  0"""
            self.cpu1_gauge.title = "".join([
                "E-CPU Usage: ",
                str(cpu_metrics_dict["E-Cluster_active"]),
                "% @ ",
                str(cpu_metrics_dict["E-Cluster_freq_Mhz"]),
                " MHz"
            ])
            self.cpu1_gauge.value = cpu_metrics_dict["E-Cluster_active"]

            """p_cpu_usage = 0
            core_count = 0
            for i in cpu_metrics_dict["p_core"]:
                p_cpu_usage += cpu_metrics_dict["P-Cluster" + str(i) + "_active"]
                core_count += 1
            p_cpu_usage = (p_cpu_usage / core_count) if core_count > 0 else  0"""
            self.cpu2_gauge.title = "".join([
                "P-CPU Usage: ",
                str(cpu_metrics_dict["P-Cluster_active"]),
                "% @ ",
                str(cpu_metrics_dict["P-Cluster_freq_Mhz"]),
                " MHz"
            ])
            self.cpu2_gauge.value = cpu_metrics_dict["P-Cluster_active"]

            if args.show_cores:
                core_count = 0
                for i in cpu_metrics_dict["e_core"]:
                    self.e_core_gauges[core_count % 4].title = "".join([
                        "Core-" + str(i + 1) + " ",
                        str(cpu_metrics_dict["E-Cluster" + str(i) + "_active"]),
                        "%",
                    ])
                    self.e_core_gauges[core_count % 4].value = cpu_metrics_dict["E-Cluster" + str(i) + "_active"]
                    core_count += 1
                core_count = 0
                for i in cpu_metrics_dict["p_core"]:
                    #core_gauges =self.p_core_gauges if core_count < 8 else self.p_core_gauges_ext
                    core_gauges = self.p_core_gauges[int(core_count / self.max_cpu_perline)]
                    core_gauges[core_count % self.max_cpu_perline].title = "".join([
                        ("Core-" if self.p_core_count < 6 else 'C-') + str(i + 1) + " ",
                        str(cpu_metrics_dict["P-Cluster" + str(i) + "_active"]),
                        "%",
                    ])
                    core_gauges[core_count % self.max_cpu_perline].value = cpu_metrics_dict["P-Cluster" + str(i) + "_active"]
                    core_count += 1

            self.gpu_gauge.title = "".join([
                "GPU Usage: ",
                str(gpu_metrics_dict["active"]),
                "% @ ",
                str(gpu_metrics_dict["freq_MHz"]),
                " MHz"
            ])
            self.gpu_gauge.value = gpu_metrics_dict["active"]

            ane_power_W = cpu_metrics_dict["ane_W"] / args.interval
            if ane_power_W > self.ane_max_power:
                self.ane_max_power = ane_power_W
            ane_util_percent = int(
                ane_power_W / self.ane_max_power * 100)
            self.ane_gauge.title = "".join([
                "ANE Usage: ",
                str(ane_util_percent),
                "% @ ",
                '{0:.1f}'.format(ane_power_W),
                " W"
            ])
            self.ane_gauge.value = ane_util_percent

            ram_metrics_dict = get_ram_metrics_dict()

            if ram_metrics_dict["swap_total_GB"] < 0.1:
                self.ram_gauge.title = "".join([
                    "RAM Usage: ",
                    str(ram_metrics_dict["used_GB"]),
                    "/",
                    str(ram_metrics_dict["total_GB"]),
                    "GB - swap inactive"
                ])
            else:
                self.ram_gauge.title = "".join([
                    "RAM Usage: ",
                    str(ram_metrics_dict["used_GB"]),
                    "/",
                    str(ram_metrics_dict["total_GB"]),
                    "GB",
                    " - swap:",
                    str(ram_metrics_dict["swap_used_GB"]),
                    "/",
                    str(ram_metrics_dict["swap_total_GB"]),
                    "GB"
                ])
            self.ram_gauge.value = ram_metrics_dict["free_percent"]

            """

            ecpu_bw_percent = int(
                (bandwidth_metrics["ECPU DCS RD"] + bandwidth_metrics[
                    "ECPU DCS WR"]) / args.interval / max_cpu_bw * 100)
            ecpu_read_GB = bandwidth_metrics["ECPU DCS RD"] / \
                            args.interval
            ecpu_write_GB = bandwidth_metrics["ECPU DCS WR"] / \
                            args.interval
            ecpu_bw_gauge.title = "".join([
                "E-CPU: ",
                '{0:.1f}'.format(ecpu_read_GB + ecpu_write_GB),
                "GB/s"
            ])
            ecpu_bw_gauge.value = ecpu_bw_percent

            pcpu_bw_percent = int(
                (bandwidth_metrics["PCPU DCS RD"] + bandwidth_metrics[
                    "PCPU DCS WR"]) / args.interval / max_cpu_bw * 100)
            pcpu_read_GB = bandwidth_metrics["PCPU DCS RD"] / \
                            args.interval
            pcpu_write_GB = bandwidth_metrics["PCPU DCS WR"] / \
                            args.interval
            pcpu_bw_gauge.title = "".join([
                "P-CPU: ",
                '{0:.1f}'.format(pcpu_read_GB + pcpu_write_GB),
                "GB/s"
            ])
            pcpu_bw_gauge.value = pcpu_bw_percent

            gpu_bw_percent = int(
                (bandwidth_metrics["GFX DCS RD"] + bandwidth_metrics["GFX DCS WR"]) / max_gpu_bw * 100)
            gpu_read_GB = bandwidth_metrics["GFX DCS RD"]
            gpu_write_GB = bandwidth_metrics["GFX DCS WR"]
            gpu_bw_gauge.title = "".join([
                "GPU: ",
                '{0:.1f}'.format(gpu_read_GB + gpu_write_GB),
                "GB/s"
            ])
            gpu_bw_gauge.value = gpu_bw_percent

            media_bw_percent = int(
                bandwidth_metrics["MEDIA DCS"] / args.interval / max_media_bw * 100)
            media_bw_gauge.title = "".join([
                "Media: ",
                '{0:.1f}'.format(
                    bandwidth_metrics["MEDIA DCS"] / args.interval),
                "GB/s"
            ])
            media_bw_gauge.value = media_bw_percent

            total_bw_GB = (
                                    bandwidth_metrics["DCS RD"] + bandwidth_metrics["DCS WR"]) / args.interval
            bw_gauges.title = "".join([
                "Memory Bandwidth: ",
                '{0:.2f}'.format(total_bw_GB),
                " GB/s (R:",
                '{0:.2f}'.format(
                    bandwidth_metrics["DCS RD"] / args.interval),
                "/W:",
                '{0:.2f}'.format(
                    bandwidth_metrics["DCS WR"] / args.interval),
                " GB/s)"
            ])
            if args.show_cores:
                bw_gauges_ext = memory_gauges.items[2]
                bw_gauges_ext.title = "Memory Bandwidth:"
            """

            package_power_W = cpu_metrics_dict["package_W"] / \
                                args.interval
            if package_power_W > self.package_peak_power:
                self.package_peak_power = package_power_W
            self.avg_package_power_list.append(package_power_W)
            avg_package_power = get_avg(self.avg_package_power_list)
            self.power_charts.title = "".join([
                "CPU+GPU+ANE Power: ",
                '{0:.2f}'.format(package_power_W),
                "W (avg: ",
                '{0:.2f}'.format(avg_package_power),
                "W peak: ",
                '{0:.2f}'.format(self.package_peak_power),
                "W) throttle: ",
                thermal_throttle,
            ])

            cpu_power_W = cpu_metrics_dict["cpu_W"] / args.interval
            if cpu_power_W > self.cpu_peak_power:
                self.cpu_peak_power = cpu_power_W
            if cpu_power_W > self.cpu_max_power:
                self.cpu_max_power = cpu_power_W
            cpu_power_percent = int(
                cpu_power_W / self.cpu_max_power * 100)                   
            self.avg_cpu_power_list.append(cpu_power_W)
            avg_cpu_power = get_avg(self.avg_cpu_power_list)
            self.cpu_power_chart.title = "".join([
                "CPU: ",
                '{0:.2f}'.format(cpu_power_W),
                "W (avg: ",
                '{0:.2f}'.format(avg_cpu_power),
                "W peak: ",
                '{0:.2f}'.format(self.cpu_peak_power),
                "W)"
            ])
            self.cpu_power_chart.append(cpu_power_percent)

            gpu_power_W = cpu_metrics_dict["gpu_W"] / args.interval
            if gpu_power_W > self.gpu_peak_power:
                self.gpu_peak_power = gpu_power_W
            if gpu_power_W > self.gpu_max_power:
                self.gpu_max_power = gpu_power_W
            gpu_power_percent = int(
                gpu_power_W / self.gpu_max_power * 100)
            self.avg_gpu_power_list.append(gpu_power_W)
            avg_gpu_power = get_avg(self.avg_gpu_power_list)
            self.gpu_power_chart.title = "".join([
                "GPU: ",
                '{0:.2f}'.format(gpu_power_W),
                "W (avg: ",
                '{0:.2f}'.format(avg_gpu_power),
                "W peak: ",
                '{0:.2f}'.format(self.gpu_peak_power),
                "W)"
            ])
            self.gpu_power_chart.append(gpu_power_percent)

            def format_number(number):
                return humanize.naturalsize(number)

            disk_read_iops = disk_metrics_dict["read_iops"]
            if disk_read_iops > self.disk_read_iops_peak:
                self.disk_read_iops_peak = disk_read_iops
            self.disk_read_iops_charts.title = "Read iops: "+ f'{disk_read_iops}'
            if self.disk_read_iops_charts.datapoints:
                disk_read_iops_rate = int(disk_read_iops / self.disk_read_iops_peak * 100) if self.disk_read_iops_peak > 0 else 0
            else:
                disk_read_iops_rate = 100
            self.disk_read_iops_charts.append(disk_read_iops_rate)

            disk_write_iops = disk_metrics_dict["write_iops"]
            if disk_write_iops > self.disk_write_iops_peak:
                self.disk_write_iops_peak = disk_write_iops
            self.disk_write_iops_charts.title = "Write iops: "+ f'{disk_write_iops}'
            if self.disk_write_iops_charts.datapoints:
                disk_write_iops_rate = int(disk_read_iops / self.disk_write_iops_peak * 100) if self.disk_write_iops_peak > 0 else 0
            else:
                disk_write_iops_rate = 100
            self.disk_write_iops_charts.append(disk_write_iops_rate)

            disk_read_bps = disk_metrics_dict["read_Bps"]
            if disk_read_bps > self.disk_read_bps_peak:
                self.disk_read_bps_peak = disk_read_bps
            self.disk_read_bps_charts.title = "Read : "+ f'{format_number(disk_read_bps)}/s'
            if self.disk_read_bps_charts.datapoints:
                disk_read_bps_rate = int(disk_read_bps / self.disk_read_bps_peak * 100) if self.disk_read_bps_peak > 0 else 0
            else:
                disk_read_bps_rate = 100
            self.disk_read_bps_charts.append(disk_read_bps_rate)

            disk_write_bps = disk_metrics_dict["write_Bps"]
            if disk_write_bps > self.disk_write_bps_peak:
                self.disk_write_bps_peak = disk_write_bps
            self.disk_write_bps_charts.title = "Write : "+ f'{format_number(disk_write_bps)}/s'
            if self.disk_write_bps_charts.datapoints:
                disk_write_bps_rate = int(disk_write_bps / self.disk_write_bps_peak * 100) if self.disk_write_bps_peak > 0 else 0
            else:
                disk_write_bps_rate = 100
            self.disk_write_bps_charts.append(disk_write_bps_rate)

            network_in_bps = network_metrics_dict["in_Bps"]
            if network_in_bps > self.network_in_bps_peak:
                self.network_in_bps_peak = network_in_bps
            self.network_in_bps_charts.title = "in : "+ f'{format_number(network_in_bps)}/s'
            if self.network_in_bps_charts.datapoints:
                network_in_bps_rate = int(network_in_bps / self.network_in_bps_peak * 100) if self.network_in_bps_peak > 0 else 0
            else:
                network_in_bps_rate = 100
            self.network_in_bps_charts.append(network_in_bps_rate)

            network_out_bps = network_metrics_dict["out_Bps"]
            if network_out_bps > self.network_out_bps_peak:
                self.network_out_bps_peak = network_out_bps
            self.network_out_bps_charts.title = "out : "+ f'{format_number(network_out_bps)}/s'
            if self.network_out_bps_charts.datapoints:
                network_out_bps_rate = int(network_out_bps / self.network_out_bps_peak * 100)  if self.network_out_bps_peak > 0 else 0
            else:
                network_out_bps_rate = 100
            self.network_out_bps_charts.append(network_out_bps_rate)

            self.disk_io_charts.title = ''.join([f"Disk IO  (peak R:{self.disk_read_iops_peak} W:{self.disk_write_iops_peak}",
                f" | R:{format_number(self.disk_read_bps_peak)}/s W:{format_number(self.disk_write_bps_peak)}/s)"
            ])
            self.network_io_charts.title = f"Network IO  (peak in:{format_number(self.network_in_bps_peak)}/s out:{format_number(self.network_out_bps_peak)}/s)"
            self.ui.display()

def get_avg(inlist):
    avg = sum(inlist) / len(inlist)
    return avg

def begin(stdscr):
    curses.use_default_colors()
    soc_info_dict = get_soc_info()
    view1 = None
    stdscr.nodelay(True)
    view = 1
    try:
        data = b''
        while True:
            output = powermetrics_process.stdout.readline()
            if powermetrics_process.poll() is not None:
                break
            data = data + output
            str_output = output.decode()
            if str_output.startswith('</plist>'):
                if view1 is None:
                    view1 = DefaultView(soc_info_dict=soc_info_dict,args=args)
                    clear_console()
                data = data.replace(b'\x00',b'')
                powermetrics_parse = plistlib.loads(data)
                key = stdscr.getch()
                if key > 0:
                    if key == 27:
                        print("\nStopping...")
                        break
                    elif key  == curses.KEY_LEFT:
                        args.color = (args.color - 1) if args.color > 1 else 8
                    elif key == curses.KEY_RIGHT:
                        args.color = (args.color + 1) if args.color < 8 else 1 
                    elif chr(key).lower() == 'q':
                        print("\nStopping...")
                        break
                    elif chr(key) == '1':
                        args.show_cores = False
                        if view != 1: 
                            view1.construct(soc_info_dict,args)
                        view = 1
                        clear_console()
                    elif chr(key) == '2':
                        args.show_cores = True
                        if view != 2: 
                            view1.construct(soc_info_dict,args)
                        view = 2
                        clear_console()
                    elif key == 0x12:
                        #press ctrl+r to reset max and peak values
                        view1.__init__(soc_info_dict,args)
                           
                if view == 1 or view == 2:
                    view1.display(powermetrics_parse,args)
                data = b''
            if str_output == '':
                time.sleep(0.1)

    except KeyboardInterrupt:
        print("Stopping...")

    return 

def main():
    global powermetrics_process
    print(f"\n{version} - enhanced MAC Performance monitoring CLI tool for Apple Silicon")
    print("You can update macpm by running `pip install macpm --upgrade`")
    print("Get help at `https://github.com/visualcjy/macpm`")
    print("P.S. You are recommended to run macpm with `sudo macpm`\n")
    print("\n[1/3] Loading macpm\n")
    print("\n[2/3] Starting powermetrics process\n")
    pause = os.popen("sudo echo").read()
    command = " ".join([
        "sudo nice -n",
        str(10),
        "powermetrics",
        "--samplers cpu_power,gpu_power,thermal,network,disk",
        "-f plist",
        "-i",
        str(args.interval * 1000)
    ])
    powermetrics_process = subprocess.Popen(command.split(" "), stdin=PIPE, stdout=PIPE)

    print("\n[3/3] Waiting for first reading...\n")
    print("\033[?25l")
    curses.wrapper(begin)
    print("\033[?25h")

if __name__ == "__main__":

    main()
 
    try:
        powermetrics_process.terminate()
        print("Successfully terminated powermetrics process")
    except Exception as e:
        print(e)
        powermetrics_process.terminate()
        print("Successfully terminated powermetrics process")

