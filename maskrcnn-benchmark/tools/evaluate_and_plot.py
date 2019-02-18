# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
# Set up custom environment before nearly anything else is imported
# NOTE: this should be the first import (no not reorder)
import json

from maskrcnn_benchmark.utils.env import setup_environment  # noqa F401 isort:skip


import matplotlib.pyplot as plt
# TODO import da rimettere sotto (messo qui perché sennò viene impostato 'agg') e usare plt.figure invece di plt.plot


import argparse
import os

import torch
from maskrcnn_benchmark.config import cfg
from maskrcnn_benchmark.data import make_data_loader
from maskrcnn_benchmark.engine.inference import inference
from maskrcnn_benchmark.modeling.detector import build_detection_model
from maskrcnn_benchmark.utils.checkpoint import DetectronCheckpointer
from maskrcnn_benchmark.utils.collect_env import collect_env_info
from maskrcnn_benchmark.utils.comm import synchronize, get_rank
from maskrcnn_benchmark.utils.logger import setup_logger
from maskrcnn_benchmark.utils.miscellaneous import mkdir


from bisect import bisect_left

from tqdm import tqdm

# Use parameter DATASETS.TEST to change evaluated datasets
# Example: DATASETS.TEST ('muscima_train', 'muscima_val')
# Note: value specified must be a tuple and single quotes must be used

# Use parameter MODEL.DEVICE to run on different device
# Example: MODEL.DEVICE "cpu"

def main():
    parser = argparse.ArgumentParser(description="PyTorch Object Detection Inference")
    parser.add_argument(
        "--config-file",
        # default="configs/muscima/e2e_faster_rcnn_R_50_FPN_1x_muscima_pretrained_imagenet.yaml",
        default="configs/muscima/e2e_faster_rcnn_R_50_FPN_1x_muscima.yaml",
        metavar="FILE",
        help="path to config file",
    )
    parser.add_argument("--local_rank", type=int, default=0)
    parser.add_argument("--gpu", type=int, default=0, help="gpu to use")
    parser.add_argument(
        "opts",
        help="Modify config options using the command-line",
        default=None,
        nargs=argparse.REMAINDER,
    )

    args = parser.parse_args()

    num_gpus = int(os.environ["WORLD_SIZE"]) if "WORLD_SIZE" in os.environ else 1
    distributed = num_gpus > 1

    if distributed:
        torch.cuda.set_device(args.local_rank)
        torch.distributed.init_process_group(
            backend="nccl", init_method="env://"
        )
        synchronize()

    cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)

    if args.config_file.startswith("configs/"):
        # se il file di configurazione specificato sta nella cartella configs, allora determino automaticamente la cartella di output
        pthDir = "models/" + args.config_file[len("configs/"):-len(".yaml")]
        cfg.update({"OUTPUT_DIR": pthDir})
    else:
        # se il file di configurazione non sta nella cartella configs, allora l'output deve essere specificato mediante il parametro OUTPUT_DIR
        pthDir = cfg["OUTPUT_DIR"]

    cfg.freeze()

    save_dir = pthDir
    logger = setup_logger("maskrcnn_benchmark", save_dir, get_rank())
    logger.info("Using {} GPUs".format(num_gpus))
    logger.info(cfg)

    logger.info("Collecting env info (might take some time)")
    logger.info("\n" + collect_env_info())

    pthFiles = [f for f in os.listdir(pthDir) if os.path.isfile(os.path.join(pthDir, f)) and f.endswith(".pth")]
    assert all(pthFile.endswith(".pth") for pthFile in pthFiles)
    metrics = {}
    for pthFile in pthFiles:
        pthPrefix = pthFile[:-4]
        cfg['MODEL'].update({"WEIGHT": os.path.join(pthDir, pthFile)})
        model = build_detection_model(cfg)
        model.to(cfg.MODEL.DEVICE)

        output_dir = cfg.OUTPUT_DIR
        checkpointer = DetectronCheckpointer(cfg, model, save_dir=output_dir)
        _ = checkpointer.load(cfg.MODEL.WEIGHT)

        iou_types = ("bbox",)
        if cfg.MODEL.MASK_ON:
            iou_types = iou_types + ("segm",)
        output_folders = [None] * len(cfg.DATASETS.TEST)
        dataset_names = cfg.DATASETS.TEST
        if cfg.OUTPUT_DIR:
            for idx, dataset_name in enumerate(dataset_names):
                # TODO per cambiare cartella salvataggio metriche, cambiare qui cfg.OUTPUT_DIR con altro percorso
                output_folder = os.path.join(cfg.OUTPUT_DIR, "inference", dataset_name, pthPrefix)
                mkdir(output_folder)
                output_folders[idx] = output_folder
        data_loaders_val = make_data_loader(cfg, is_train=False, is_distributed=distributed)
        for output_folder, dataset_name, data_loader_val in zip(output_folders, dataset_names, data_loaders_val):
            if not os.path.exists(os.path.join(output_folder, "coco_results.pth")):
                inference(
                    model,
                    data_loader_val,
                    dataset_name=dataset_name,
                    iou_types=iou_types,
                    box_only=cfg.MODEL.RPN_ONLY,
                    device=cfg.MODEL.DEVICE,
                    expected_results=cfg.TEST.EXPECTED_RESULTS,
                    expected_results_sigma_tol=cfg.TEST.EXPECTED_RESULTS_SIGMA_TOL,
                    output_folder=output_folder,
                )
            synchronize()

        for output_folder, dataset_name, data_loader_val in zip(output_folders, dataset_names, data_loaders_val):

            avgDistFilePath = os.path.join(output_folder, "average_distance.pth")
            if not os.path.exists(avgDistFilePath):
                AD = 0
                truths = loadPredictionsFromJsonData(data_loader_val.dataset.coco.dataset['annotations'])
                preds = loadPredictionsFromJson(os.path.join(output_folder, "bbox.json"), 0.7)

                allkeys = set(preds.keys()).union(set(truths.keys()))
                for image in allkeys:
                    if image not in truths or image not in preds:
                        AD += 1
                        continue
                    trueSeq = getLabelsSequence(*sortAnnotations(truths[image]["bboxes"], truths[image]["labels"]))
                    predSeq = getLabelsSequence(*sortAnnotations(preds[image]["bboxes"], preds[image]["labels"]))
                    AD += normalizedSequencesDistance(trueSeq, predSeq)
                AD /= len(allkeys)
                torch.save(AD, avgDistFilePath)

            AD = torch.load(avgDistFilePath)
            results = torch.load(os.path.join(output_folder, "coco_results.pth")).results['bbox']
            results["AD"] = AD
            for metric in results:
                if metric not in metrics:
                    metrics[metric] = {}
                if dataset_name not in metrics[metric]:
                    metrics[metric][dataset_name] = {}
                metrics[metric][dataset_name][pthPrefix] = results[metric]

    plot(metrics, os.path.join(cfg.OUTPUT_DIR, "inference"))


# crea grafici e li salva in vari file .png
def plot(metrics, path):
    # TODO confronto varianti diverse (per ora è solo confronto dataset diversi)
    # per ogni metrica faccio un grafico
    for metric in metrics:
        plt.title(metric)
        # per ogni dataset faccio una funzione
        for dataset_name in metrics[metric]:
            pointsX = []
            pointsY = []
            # per ogni file .pth ho un punto nella funzione
            for pthPrefix in metrics[metric][dataset_name]:
                x = int(pthPrefix[-7:])             # la x del punto è l'iterazione
                # inserisco il punto in modo che pointsX sia ordinato per x crescente
                index = bisect_left(pointsX, x)
                pointsX.insert(index, x)
                pointsY.insert(index, metrics[metric][dataset_name][pthPrefix])

            plt.plot(pointsX, pointsY, label=dataset_name)

        plt.legend(loc='lower right')
        plt.savefig(os.path.join(path, metric + ".png"))
        plt.clf()


# ordina le annotazioni per xmin crescente
def sortAnnotations(bboxes, labels):
    bboxes, labels = [list(z) for z in
                              zip(*sorted(zip(bboxes, labels), key=lambda l: l[0][0]))]
    return bboxes, labels


# ritorna una lista di liste di label: la lista esterna scandisce il tempo, ogni lista interna contiene le note suonate in un determinato istante (infatti possono esserci più note in contemporanea)
def getLabelsSequence(bboxes, labels):
    # l'ordinamento deve già essere stato fatto

    assert len(bboxes) == len(labels)
    if len(bboxes) == 0:
        return []

    # (i bbox sono in formato x1, y1, x2, y1)

    sequence = []  # lista di liste di label
    bbox = [-1, -1, -1, -1]  # bbox fasullo, è comodo per non gestire la prima iterazione del ciclo con un if (perché non esiste un bbox precedente al primo)
    for i in range(len(bboxes)):
        previousBbox = bbox
        bbox = bboxes[i]
        label = labels[i]
        if bbox[0] <= previousBbox[2]:  # bbox.x1 <= previousBbox.x2
            # se questa annotazione sta sopra o sotto all'annotazione precedente (ovvero le note sono da suonare nello stesso istante)
            sequence[-1].append(label)
        else:
            # se l'annotazione i-esima è lontana da quella precedente, metto la label in un nuovo istante di tempo
            sequence.append([label])

    return sequence


cache = {}
# ritorna la distanza tra due sequenze, non normalizzata
def sequencesDistance(trueSeq, predSeq):
    if len(trueSeq) < len(predSeq):
        trueSeq, predSeq = predSeq, trueSeq

    seqsStr = str(trueSeq) + str(predSeq)
    if seqsStr in cache:
        return cache[seqsStr]

    if len(predSeq) == 0:
        cache[seqsStr] = len([label for instant in trueSeq for label in instant])
        return cache[seqsStr]

    t = set(trueSeq[-1])  # insieme delle note nell'ultimo istante del groundtruth
    p = set(predSeq[-1])  # insieme delle note nell'ultimo istante della predizione
    cost = len(t - p) + len(p - t)  # numero di elementi per cui i due insiemi differiscono

    # ricorsione edit-distance
    cache[seqsStr] = min([sequencesDistance(trueSeq[:-1], predSeq) + len(t),
                          sequencesDistance(trueSeq, predSeq[:-1]) + len(p),
                          sequencesDistance(trueSeq[:-1], predSeq[:-1]) + cost])
    return cache[seqsStr]


def normalizedSequencesDistance(trueSeq, predSeq):
    n = max(len([label for instant in trueSeq for label in instant]), len([label for instant in predSeq for label in instant]))
    return sequencesDistance(trueSeq, predSeq) / n


def loadPredictionsFromJsonData(js, th=None):
    preds = {}
    for ann in js:
        if th is not None and ann['score'] < th:
            continue
        an_id = ann['image_id']
        if an_id in preds:
            preds[an_id]['bboxes'].append(ann['bbox'])
            preds[an_id]['labels'].append(ann['category_id'])
        else:
            preds[an_id] = {}
            preds[an_id]['bboxes'] = [ann['bbox']]
            preds[an_id]['labels'] = [ann['category_id']]

    return preds


def loadPredictionsFromJson(jsonPath, th=0.7):
    with open(jsonPath, "r") as jsonFile:
        js = json.load(jsonFile)
    return loadPredictionsFromJsonData(js, th)


if __name__ == "__main__":
    main()
