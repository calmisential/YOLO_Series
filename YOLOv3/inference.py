import torch
import cv2
import os
import numpy as np

from YOLOv3.nms import apply_nms
from draw import draw_boxes_on_image
from utils import ResizeWithPad, letter_box
from torchvision.transforms.functional import to_tensor


def generate_grid_index(length, device):
    X = torch.arange(start=0, end=length, step=1, dtype=torch.float32, device=device)
    Y = torch.arange(start=0, end=length, step=1, dtype=torch.float32, device=device)
    X, Y = torch.meshgrid(X, Y)
    X = torch.reshape(X, shape=(-1, 1))
    Y = torch.reshape(Y, shape=(-1, 1))
    return torch.cat((X, Y), dim=-1)


def predict_bounding_bbox(cfg, feature_map, anchors, idx, device, is_training=False):
    num_classes = cfg["Model"]["num_classes"]
    N, C, H, W = feature_map.size()
    feature_map = torch.reshape(feature_map, shape=(N, H, W, -1))
    area = H * W
    pred = torch.reshape(feature_map, shape=(N, area * 3, -1))
    tx_ty, tw_th, confidence, class_prob = torch.split(pred, split_size_or_sections=[2, 2, 1, num_classes], dim=-1)
    confidence = torch.sigmoid(confidence)
    class_prob = torch.sigmoid(class_prob)

    center_index = generate_grid_index(length=H, device=device)
    center_index = torch.tile(center_index, dims=[1, 3])
    center_index = torch.reshape(center_index, shape=(1, -1, 2))

    center_coord = center_index + torch.sigmoid(tx_ty)
    box_xy = center_coord / H
    anchors = anchors[idx * 3:(idx + 1) * 3, :]
    anchors /= cfg["Train"]["input_size"]
    anchors = torch.tile(anchors, dims=[area, 1])
    bw_bh = anchors * torch.exp(tw_th)
    box_wh = bw_bh

    # reshape
    center_index = torch.reshape(center_index, shape=(-1, H, W, 3, 2))
    box_xy = torch.reshape(box_xy, shape=(-1, H, W, 3, 2))
    box_wh = torch.reshape(box_wh, shape=(-1, H, W, 3, 2))
    feature_map = torch.reshape(feature_map, shape=(-1, H, W, 3, num_classes + 5))

    if is_training:
        return box_xy, box_wh, center_index, feature_map
    else:
        return box_xy, box_wh, confidence, class_prob


class Inference:
    def __init__(self, cfg, outputs, input_image_shape, device):
        self.cfg = cfg
        self.device = device
        self.outputs = outputs
        self.input_image_h = input_image_shape[0]
        self.input_image_w = input_image_shape[1]

        self.anchors = cfg["Train"]["anchor"]
        self.anchors = torch.tensor(self.anchors, dtype=torch.float32)
        self.anchors = torch.reshape(self.anchors, shape=(-1, 2))

    def _yolo_post_process(self, feature, scale_type):
        box_xy, box_wh, confidence, class_prob = predict_bounding_bbox(self.cfg, feature, self.anchors, scale_type,
                                                                       self.device, is_training=False)
        boxes = self._boxes_to_original_image(box_xy, box_wh)
        boxes = torch.reshape(boxes, shape=(-1, 4))
        boxes_scores = confidence * class_prob
        boxes_scores = torch.reshape(boxes_scores, shape=(-1, self.cfg["Model"]["num_classes"]))
        return boxes, boxes_scores

    def _boxes_to_original_image(self, box_xy, box_wh):
        x = box_xy[..., 0:1]
        y = box_xy[..., 1:2]
        w = box_wh[..., 0:1]
        h = box_wh[..., 1:2]
        x, y, w, h = ResizeWithPad(cfg=self.cfg, h=self.input_image_h, w=self.input_image_w).resized_to_raw(x, y, w, h)
        xmin = x - w / 2
        ymin = y - h / 2
        xmax = x + w / 2
        ymax = y + h / 2
        boxes = torch.cat((xmin, ymin, xmax, ymax), dim=-1)
        return boxes

    def get_results(self):
        boxes_list = list()
        boxes_scores_list = list()
        for i in range(3):
            boxes, boxes_scores = self._yolo_post_process(feature=self.outputs[i],
                                                          scale_type=i)
            boxes_list.append(boxes)
            boxes_scores_list.append(boxes_scores)
        boxes = torch.cat(boxes_list, dim=0)
        scores = torch.cat(boxes_scores_list, dim=0)
        return apply_nms(self.cfg, boxes, scores, self.device)


def test_pipeline(cfg, model, image_path, device):
    image = cv2.imread(image_path, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    h, w, c = image.shape
    image, _, _ = letter_box(image, (cfg["Train"]["input_size"], cfg["Train"]["input_size"]))
    image = to_tensor(image)
    image = torch.unsqueeze(image, dim=0)
    outputs = model(image)
    boxes, scores, classes = Inference(cfg=cfg, outputs=outputs, input_image_shape=(h, w), device=device).get_results()
    boxes = boxes.detach().numpy()
    scores = scores.detach().numpy()
    classes = classes.detach().numpy()

    image_with_boxes = draw_boxes_on_image(cfg, image_path, boxes, scores, classes)

    # 保存检测结果
    save_dir = "./detect/detected_" + os.path.basename(image_path).split(".")[0] + ".jpg"
    cv2.imwrite(save_dir, image_with_boxes)