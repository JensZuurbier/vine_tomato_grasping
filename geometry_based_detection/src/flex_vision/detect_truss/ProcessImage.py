#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import warnings
import numpy as np
import cv2
import json
import sys
import pandas as pd
from matplotlib import pyplot as plt
from pathlib import Path

sys.path.append(os.getcwd())

from flex_vision.detect_truss.filter_segments import filter_segments
from flex_vision.detect_truss.detect_peduncle_2 import detect_peduncle, visualize_skeleton, get_node_coord
from flex_vision.detect_truss.detect_tomato import detect_tomato
from flex_vision.detect_truss.segment_image import segment_truss, segment_truss_2
import flex_vision.detect_truss.settings as settings

sys.path.append("../")

from flex_vision.utils import imgpy
from flex_vision.utils.geometry import Point2D, Transform, points_from_coords, coords_from_points
from flex_vision.utils.timer import Timer

from flex_vision.utils.util import make_dirs, load_rgb, save_img, save_fig, figure_to_image
from flex_vision.utils.util import stack_segments, change_brightness
from flex_vision.utils.util import plot_timer, plot_grasp_location, plot_image, plot_features, plot_segments
from flex_vision.utils.util import save_images, find_grasp_coords_and_angles, find_grasp_point_end_peduncle, find_grasp_point_com
from flex_vision.utils.util import find_grasp_point_middle_image
from flex_vision.utils.util import find_mean_of_array, find_px_per_mm
from flex_vision.utils.util import create_bboxed_images, widen_bboxes

from flex_vision.bbox_detection.bbox_detection import get_detection_model
from flex_vision.bbox_detection.bbox_detection import predict_truss

warnings.filterwarnings('error', category=FutureWarning)

class ProcessImage(object):
    version = '0.1'

    # frame ids
    ORIGINAL_FRAME_ID = 'original'
    LOCAL_FRAME_ID = 'local'

    name_space = 'main'  # used for timer

    def __init__(self,
                 use_truss=True,
                 save=False,
                 com_grasp=True,
                 pwd='',
                 name='tomato',
                 ext='pdf',
                 command=None
                 ):

        self.ext = ext
        self.save = save
        self.use_truss = use_truss
        self.pwd = pwd
        self.name = name
        self.com_grasp = com_grasp
        self.command = command

        self.scale = None
        self.img_rgb = None
        self.shape = None
        self.px_per_mm = None

        self.background = None
        self.tomato = None
        self.peduncle = None

        self.grasp_point = None
        self.grasp_angle_local = None
        self.grasp_angle_global = None

        self.skeleton_img = None
        self.tomato_size = None
        self.depth = None
        self.peduncle_width = 3.5 # [mm]

        self.settings = settings.initialize_all()

    def add_image(self, img_rgb, tomato_info=None, name=None, depth_data=None):

        # TODO: scaling is currently not supported, would be interesting to reduce computing power

        self.scale = 1.0
        self.img_rgb = img_rgb
        self.shape = img_rgb.shape[:2]
        self.px_per_mm = tomato_info['px_per_mm']
        self.tomato_size = tomato_info['tomato_size']
        self.depth = depth_data

        self.grasp_point = None
        self.grasp_angle_local = None
        self.grasp_angle_global = None

        if 'bbox' in tomato_info.keys():
            self.bbox_image = tomato_info['bbox']
        else:
            self.bbox_image = None

        if name is not None:
            self.name = name

    @Timer("color space", name_space)
    def color_space(self, compute_a=True):
        pwd = os.path.join(self.pwd, '01_color_space')
        self.img_hue = cv2.cvtColor(self.img_rgb, cv2.COLOR_RGB2HSV)[:, :, 0]
        ########TODO: remove
        self.img_hsv = cv2.cvtColor(self.img_rgb, cv2.COLOR_RGB2HSV)
        self.img_sat = cv2.cvtColor(self.img_rgb, cv2.COLOR_RGB2HSV)[:, :, 1]
        self.img_val = cv2.cvtColor(self.img_rgb, cv2.COLOR_RGB2HSV)[:, :, 2]
        ########
        self.img_a = cv2.cvtColor(self.img_rgb, cv2.COLOR_RGB2LAB)[:, :, 1]  # L: 0 to 255, a: 0 to 255, b: 0 to 255

        if self.save:
            save_img(self.img_hue, pwd, self.name + '_h_raw', vmin=0, vmax=180)
            save_img(self.img_a, pwd, self.name + '_a_raw', vmin=1, vmax=255)

            save_img(self.img_hue, pwd, self.name + '_h', color_map='HSV', vmin=0, vmax=180)
            save_img(self.img_a, pwd, self.name + '_a', color_map='Lab')

    @Timer("segmentation", name_space)
    def segment_image(self, radius=None, save_segment=False):
        """segment image based on hue and a color components.

        Keyword arguments:
        radius -- hue circle radius (see https://surfdrive.surf.nl/files/index.php/s/StoH7xA87zUxl79 page 16)
        """

        if radius is None:
            pwd = os.path.join(self.pwd, '02_segment')
        else:
            pwd = os.path.join(self.pwd, '02_segment', str(radius))
            self.settings['segment_image']['hue_radius'] = radius

        success = True
        # background, tomato, peduncle = segment_truss(self.img_hue,
        #                                              img_a=self.img_a,
        #                                              img_sat = self.img_sat,
        #                                              img_val = self.img_val,
        #                                              my_settings=self.settings['segment_image'],
        #                                              save=self.save,
        #                                              pwd=pwd,
        #                                              name=self.name)

        background, tomato, peduncle = segment_truss_2(self.img_hsv, self.img_rgb, pwd=self.pwd, name=self.name)     

        if save_segment:
            images = [background, tomato, peduncle]
            path = os.path.join(self.pwd, '02_segment')
            save_images(images, path, self.name)

        self.background = background
        self.tomato = tomato
        self.peduncle = peduncle

        if self.tomato.sum() == 0:
            warnings.warn("Segment truss: no pixel has been classified as tomato!")
            success = False

        if self.peduncle.sum() == 0:
            warnings.warn("Segment truss: no pixel has been classified as peduncle!")
            success = False

        if self.save:
            self.save_results(self.name, pwd=pwd)

        return success

    @Timer("filtering", name_space)
    def filter_image(self, folder_name=None):
        """ remove small pixel blobs from the determined segments

        Keyword arguments:
        folder_name -- name of folder where results are stored
        """
        pwd = os.path.join(self.pwd, '03_filter')
        if folder_name is not None:
            pwd = os.path.join(pwd, folder_name)

        tomato, peduncle, background = filter_segments(self.tomato,
                                                       self.peduncle,
                                                       self.background,
                                                       settings=self.settings['filter_segments'])

        self.tomato = tomato
        self.peduncle = peduncle
        self.background = background

        if self.save:
            self.save_results(self.name, pwd=pwd)

        if self.tomato.sum() == 0:
            warnings.warn("filter segments: no pixel has been classified as tomato")
            return False

        return True

    @Timer("cropping", name_space)
    def rotate_cut_img(self):
        """crop the image"""
        pwd = os.path.join(self.pwd, '04_crop')

        if self.peduncle.sum() == 0:
            warnings.warn("Cannot rotate based on peduncle, since it does not exist!")
            angle = 0
        else:
            angle = imgpy.compute_angle(self.peduncle)  # [rad]

        tomato_rotate = imgpy.rotate(self.tomato, -angle)
        peduncle_rotate = imgpy.rotate(self.peduncle, -angle)
        truss_rotate = imgpy.add(tomato_rotate, peduncle_rotate)

        if truss_rotate.sum() == 0:
            warnings.warn("Cannot crop based on truss segment, since it does not exist!")
            return False

        bbox = imgpy.bbox(truss_rotate)
        x = bbox[0]  # col
        y = bbox[1]  # rows to upper left corner

        translation = [x, y]
        xy_shape = [self.shape[1], self.shape[0]] # [width, height] 
        self.transform = Transform(self.ORIGINAL_FRAME_ID, self.LOCAL_FRAME_ID, xy_shape, angle=-angle,
                                   translation=translation)

        self.bbox = bbox
        self.angle = angle

        self.tomato_crop = imgpy.cut(tomato_rotate, self.bbox)
        self.peduncle_crop = imgpy.cut(peduncle_rotate, self.bbox)
        self.img_rgb_crop = imgpy.crop(self.img_rgb, angle=-angle, bbox=bbox)
        self.truss_crop = imgpy.cut(truss_rotate, self.bbox)

        if self.save:
            img_rgb = self.get_rgb(local=True)
            save_img(img_rgb, pwd=pwd, name=self.name)
            # self.save_results(self.name, pwd=pwd, local=True)

    @Timer("tomato detection", name_space)
    def detect_tomatoes(self):
        """detect tomatoes based on the truss segment"""
        pwd = os.path.join(self.pwd, '05_tomatoes')

        if self.tomato_crop.sum() == 0:
            warnings.warn("Detect tomato: no pixel has been classified as truss!")
            return False

        if self.save:
            img_bg = self.img_rgb_crop
        else:
            img_bg = self.img_rgb_crop

        xy_peduncle = coords_from_points(self.junction_points, self.LOCAL_FRAME_ID)
        
        centers, radii, com = detect_tomato(self.tomato_crop,
                                            self.settings['detect_tomato'],
                                            px_per_mm=self.px_per_mm,
                                            img_rgb=img_bg,
                                            save=self.save,
                                            pwd=pwd,
                                            name=self.name,
                                            save_tomato=False,
                                            tomato_size=self.tomato_size,
                                            xy_peduncle=xy_peduncle)

        # convert obtained coordinated to two-dimensional points linked to a coordinate frame
        self.radii = radii
        self.centers = points_from_coords(centers, self.LOCAL_FRAME_ID, self.transform)
        self.com = Point2D(com, self.LOCAL_FRAME_ID, self.transform)

        if self.com is None:
            return False
        else:
            return True

    @Timer("peduncle detection", name_space)
    def detect_peduncle(self):
        """Detect the peduncle and junctions"""
        pwd = os.path.join(self.pwd, '06_peduncle')
        success = True

        if self.save:
            img_bg = change_brightness(self.get_segmented_image(local=True), 0.85)
        else:
            img_bg = self.img_rgb_crop

        mask, branch_data, junc_coords, end_coords = detect_peduncle(self.peduncle_crop,
                                                                     self.settings['detect_peduncle'],
                                                                     px_per_mm=self.px_per_mm,
                                                                     save=self.save,
                                                                     bg_img=img_bg,
                                                                     name=self.name,
                                                                     pwd=pwd,
                                                                     save_skeleton=False)
        # convert to 2D points
        peduncle_points = points_from_coords(np.argwhere(mask)[:, (1, 0)], self.LOCAL_FRAME_ID, self.transform)
        junction_points = points_from_coords(junc_coords, self.LOCAL_FRAME_ID, self.transform)
        end_points = points_from_coords(end_coords, self.LOCAL_FRAME_ID, self.transform)

        # generate peduncle image
        xy_peduncle = coords_from_points(peduncle_points, self.LOCAL_FRAME_ID)
        rc_peduncle = np.around(np.array(xy_peduncle)).astype(int)[:,(1, 0)]

        # img_shape = self.img_rgb.shape[:2]
        img_shape = img_bg.shape[:2]
        shape = (max(img_shape),max(img_shape))

        skeleton_img = np.zeros(shape, dtype=np.uint8)
        skeleton_img[rc_peduncle[:, 0], rc_peduncle[:, 1]] = 1

        for branch_type in branch_data:
            for i, branch in enumerate(branch_data[branch_type]):
                for lbl in ['coords', 'src_node_coord', 'dst_node_coord', 'center_node_coord']:

                    if lbl == 'coords':
                        branch_data[branch_type][i][lbl] = points_from_coords(branch[lbl], self.LOCAL_FRAME_ID,
                                                                              self.transform)
                    else:
                        branch_data[branch_type][i][lbl] = Point2D(branch[lbl], self.LOCAL_FRAME_ID, self.transform)

        self.junction_points = junction_points
        self.end_points = end_points
        self.peduncle_points = peduncle_points
        self.branch_data = branch_data
        self.skeleton_img = skeleton_img

        return success

    # @Timer("detect grasp location", name_space)
    # def detect_grasp_location(self):
    #     """Determine grasp location based on peduncle, junction and com information"""
    #     pwd = os.path.join(self.pwd, '07_grasp')
    #     success = True

    #     settings = self.settings['compute_grasp']

    #     # set dimensions
    #     if self.px_per_mm is not None:
    #         minimum_grasp_length_px = self.px_per_mm * settings['grasp_length_min_mm']
    #     else:
    #         minimum_grasp_length_px = settings['grasp_length_min_px']

    #     points_keep = []
    #     branches_i = []
    #     for branch_i, branch in enumerate(self.branch_data['junction-junction']):
    #         if branch['length'] > minimum_grasp_length_px:
    #             src_node_dist = branch['src_node_coord'].dist(branch['coords'])
    #             dst_node_dist = branch['dst_node_coord'].dist(branch['coords'])
    #             is_true = np.logical_and((np.array(dst_node_dist) > 0.5 * minimum_grasp_length_px), (
    #                     np.array(src_node_dist) > 0.5 * minimum_grasp_length_px))

    #             branch_points_keep = np.array(branch['coords'])[is_true].tolist()
    #             points_keep.extend(branch_points_keep)
    #             branches_i.extend([branch_i] * len(branch_points_keep))

    #     if len(branches_i) > 0:
    #         i_grasp = np.argmin(self.com.dist(points_keep))
    #         grasp_point = points_keep[i_grasp]
    #         branch_i = branches_i[i_grasp]

    #         grasp_angle_local = np.deg2rad(self.branch_data['junction-junction'][branch_i]['angle'])
    #         grasp_angle_global = -self.angle + grasp_angle_local

    #         self.grasp_point = grasp_point
    #         self.grasp_angle_local = grasp_angle_local
    #         self.grasp_angle_global = grasp_angle_global

    #     else:
    #         warnings.warn('Did not detect a valid grasping branch')

    #         if self.save:
    #             img_rgb = self.img_rgb_crop
    #             save_img(img_rgb, pwd=pwd, name=self.name)
    #             img_rgb = self.img_rgb
    #             save_img(img_rgb, pwd=pwd, name=self.name + '_g')
    #         return False

    #     if self.save:
    #         open_dist_px = settings['open_dist_mm'] * self.px_per_mm
    #         finger_thickness_px = settings['finger_thinkness_mm'] * self.px_per_mm
    #         brightness = 0.85

    #         for frame_id in [self.LOCAL_FRAME_ID, self.ORIGINAL_FRAME_ID]:
    #             grasp_coord = self.grasp_point.get_coord(frame_id)

    #             if frame_id == self.LOCAL_FRAME_ID:
    #                 grasp_angle = self.grasp_angle_local
    #                 img_rgb = self.img_rgb_crop

    #             elif frame_id == self.ORIGINAL_FRAME_ID:
    #                 grasp_angle = self.grasp_angle_global
    #                 img_rgb = self.img_rgb

    #             img_rgb_bright = change_brightness(img_rgb, brightness)
    #             branch_image = np.zeros(img_rgb_bright.shape[0:2], dtype=np.uint8)
    #             coords = np.rint(coords_from_points(points_keep, frame_id)).astype(np.int)
    #             branch_image[coords[:, 1], coords[:, 0]] = 255

    #             if frame_id == self.ORIGINAL_FRAME_ID:
    #                 kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    #                 branch_image = cv2.dilate(branch_image, kernel, iterations=1)

    #             visualize_skeleton(img_rgb_bright, branch_image, show_nodes=False, skeleton_color=(0, 0, 0),
    #                                skeleton_width=4)
    #             plot_grasp_location(grasp_coord, grasp_angle, finger_width=minimum_grasp_length_px,
    #                                 finger_thickness=finger_thickness_px, finger_dist=open_dist_px, pwd=pwd,
    #                                 name=self.name + '_' + frame_id, linewidth=3)

    #     return success

    @Timer("detect grasp location", name_space)
    def detect_grasp_location(self):
        success = True

        grasp_img = self.skeleton_img.copy()
        coord_junc, coord_end = get_node_coord(self.skeleton_img)
        all_coords = np.vstack((coord_junc,coord_end))

        for coord in all_coords:
            coord = coord.astype(int)

            radius = int(np.mean(grasp_img.shape) * 0.02)

            # ensure region doesn't exceed image shape
            x_start = max(0,coord[1]-radius)
            x_end = min(grasp_img.shape[0],coord[1]+radius)
            y_start = max(0,coord[0]-radius)
            y_end = min(grasp_img.shape[1],coord[0]+radius)

            area_x = np.arange(x_start,x_end)
            area_y = np.arange(y_start,y_end)
            for i in range(len(area_x)):
                for j in range(len(area_y)):
                    grasp_img[area_x[i]][area_y[j]] = 0
        
        grasp_coords, angles, grasp_area_lengths = find_grasp_coords_and_angles(grasp_img)

        if self.com_grasp:
            # when close_to_COM true, grasp point closest to COM is chosen, else grasp point closest to peduncle mid_point
            close_to_COM = False

            if close_to_COM:
                mid_point = self.com.get_coord(self.LOCAL_FRAME_ID)
            else:
                _ = self.get_peduncle()
                mid_point = self.peduncle_mid_point.get_coord(self.LOCAL_FRAME_ID)
            grasp_pixel, angle = find_grasp_point_com(grasp_coords, angles, mid_point)
        
        else:
            if self.command == 'detect_grasp_point_2' or self.command == 'detect_grasp_point_close':
                grasp_pixel, angle = find_grasp_point_middle_image(grasp_coords, angles, self.shape, self.bbox)
            else:
                peduncle_xy = coords_from_points(self.peduncle_points, self.LOCAL_FRAME_ID)
                grasp_pixel, angle = find_grasp_point_end_peduncle(grasp_coords, angles, self.peduncle_crop, grasp_area_lengths)

        grasp_angle_local = angle
        grasp_angle_global = -self.angle + grasp_angle_local

        # convert to 2D points
        grasp_point = points_from_coords(grasp_pixel, self.LOCAL_FRAME_ID, self.transform)
        grasp_img_points = points_from_coords(np.argwhere(grasp_img)[:, (1, 0)], self.LOCAL_FRAME_ID, self.transform)
        grasp_points = points_from_coords(grasp_coords, self.LOCAL_FRAME_ID, self.transform)

        self.grasp_angle_local = grasp_angle_local
        self.grasp_angle_global = grasp_angle_global
        self.grasp_point = grasp_point
        self.grasp_img_points = grasp_img_points
        self.grasp_points = grasp_points

        return success

    def crop(self, image):
        return imgpy.crop(image, angle=-self.angle, bbox=self.bbox)

    def get_tomatoes(self, local=False):
        if local:
            target_frame_id = self.LOCAL_FRAME_ID
        else:
            target_frame_id = self.ORIGINAL_FRAME_ID

        if self.centers is None:
            radii = []
            xy_centers = [[]]
            xy_com = []
            row = []
            col = []

        else:
            xy_centers = coords_from_points(self.centers, target_frame_id)
            xy_com = self.com.get_coord(target_frame_id)
            radii = self.radii.tolist()

            row = [xy_center[1] for xy_center in xy_centers]
            col = [xy_center[0] for xy_center in xy_centers]

        tomato = {'centers': xy_centers, 'radii': radii, 'com': xy_com, "row": row, "col": col}
        return tomato

    def get_peduncle(self, local=False):
        """Returns a dictionary containing a description of the peduncle"""

        if local:
            frame_id = self.LOCAL_FRAME_ID
        else:
            frame_id = self.ORIGINAL_FRAME_ID

        peduncle_xy = coords_from_points(self.peduncle_points, frame_id)
        junc_xy = coords_from_points(self.junction_points, frame_id)
        end_xy = coords_from_points(self.end_points, frame_id)
        
        peduncle_dx = abs(end_xy[0][0] - end_xy[1][0])
        peduncle_dy = abs(end_xy[0][1] - end_xy[1][1])
        peduncle_length = np.sqrt(peduncle_dx**2 + peduncle_dy**2)
        peduncle_middle_point = [min(end_xy[0][0], end_xy[1][0]) + peduncle_dx/2, min(end_xy[0][1], end_xy[1][1])+ peduncle_dy/2]
        peduncle = {'junctions': junc_xy, 'ends': end_xy, 'peduncle_length': peduncle_length, 'peduncle_middle_point': peduncle_middle_point, 'peduncle': peduncle_xy}
        
        self.peduncle_mid_point = Point2D(peduncle_middle_point, self.ORIGINAL_FRAME_ID, self.transform)
        
        return peduncle

    # def get_grasp_location(self, local=False):
    #     """Returns a dictionary containing a description of the peduncle"""
    #     if local:
    #         frame_id = self.LOCAL_FRAME_ID
    #         angle = self.grasp_angle_local
    #     else:
    #         frame_id = self.ORIGINAL_FRAME_ID
    #         angle = self.grasp_angle_global
    #     if self.grasp_point is not None:
    #         xy = self.grasp_point.get_coord(frame_id)
    #         grasp_pixel = np.around(xy).astype(int)
    #         row = grasp_pixel[1]
    #         col = grasp_pixel[0]
    #     else:
    #         row = 0      
    #         col = 0      
    #         xy = []

    #     grasp_location = {"xy": xy, "row": int(row), "col": int(col), "angle": angle}
    #     return grasp_location

    def get_grasp_points(self, local=False, show_grasp_area=False):
        if local:
            frame_id = self.LOCAL_FRAME_ID
            angle = self.grasp_angle_local
        else:
            frame_id = self.ORIGINAL_FRAME_ID
            angle = self.grasp_angle_global
        
        if self.grasp_point is not None:
            [xy] = coords_from_points(self.grasp_point, frame_id)
            grasp_pixel = np.around(xy).astype(int)
            x = grasp_pixel[0]
            y = grasp_pixel[1]

            grasp = {'bboxed_image':{"xy": xy, "x": int(x), "y": int(y), "angle": angle}}

            if self.bbox_image is not None:
                full_size_image_xy = [int(grasp_pixel[0] + self.bbox_image[0]), int(grasp_pixel[1] + self.bbox_image[1])]
                full_size_image_x = full_size_image_xy[0]
                full_size_image_y = full_size_image_xy[1]
            
                grasp['full_size_image'] = {"xy": full_size_image_xy, "x": full_size_image_x, "y": full_size_image_y, "angle": angle}
        
        if not show_grasp_area:
            grasp_img = grasp_points = None
        else:
            grasp_points = coords_from_points(self.grasp_points, frame_id)

            grasp_img_points = coords_from_points(self.grasp_img_points, frame_id)

            rc_peduncle = np.around(np.array(grasp_img_points)).astype(int)[:,(1, 0)]

            img_shape = self.img_rgb_crop.shape[:2]
            shape = (max(img_shape),max(img_shape))

            grasp_img = np.zeros(shape, dtype=np.uint8)
            grasp_img[rc_peduncle[:, 0], rc_peduncle[:, 1]] = 1
        
        return grasp, grasp_img, grasp_points

    # def get_distance_to_camera(self, grasp=None, tomato_info=None):
        
    #     depth_image = self.depth

    #     dx = grasp['full_size_image']['x'] - depth_image.shape[1]/2
    #     dy = grasp['full_size_image']['y'] - depth_image.shape[0]/2

    #     dx_in_mm = dx/self.px_per_mm
    #     dy_in_mm = dy/self.px_per_mm

    #     xy = grasp['full_size_image']['xy']
    #     x = xy[0]
    #     y = xy[1]
    #     window_size = round(self.peduncle_width/2 * self.px_per_mm)
        
    #     z_array = depth_image[x-window_size:x+window_size+1, y-window_size:y+window_size+1]
    #     mean = np.average(z_array)

    #     count = 0
    #     _sum = 0
    #     for i in range(len(z_array)):
    #         for j in range(len(z_array[i])):
    #             if abs(z_array[i][j] - mean) > 0.1*mean:
    #                 continue
    #             else:
    #                 count += 1
    #                 _sum += z_array[i][j]
        
    #     dz_in_mm = _sum/count

    #     distance = {'dx': dx_in_mm, 'dy': dy_in_mm, 'dz': dz_in_mm}

    #     return distance

    def get_skeleton_img(self, img_shape, local=False):
        if local:
            frame_id = self.LOCAL_FRAME_ID
        else:
            frame_id = self.ORIGINAL_FRAME_ID
        
        # generate peduncle image
        xy_peduncle = coords_from_points(self.peduncle_points, frame_id)
        rc_peduncle = np.around(np.array(xy_peduncle)).astype(int)[:,(1, 0)]

        shape = (max(img_shape),max(img_shape))

        skeleton_img = np.zeros(shape, dtype=np.uint8)
        skeleton_img[rc_peduncle[:, 0], rc_peduncle[:, 1]] = 1

        return skeleton_img

    def get_object_features(self, tomato_info=None):
        """
        Returns a dictionary containing the grasp_location, peduncle, and tomato
        """
        tomatoes = None
        # tomatoes = self.get_tomatoes()
        peduncle = self.get_peduncle()
        grasp_location, __, __ = self.get_grasp_points()
        
        object_feature = {
            "tomato_info": tomato_info,
            "grasp_location": grasp_location,
            "tomato": tomatoes,
            "peduncle": peduncle
        }
        return object_feature

    def get_tomato_visualization(self, local=False):
        if local is True:
            frame = self.LOCAL_FRAME_ID
            zoom = True
        else:
            frame = self.ORIGINAL_FRAME_ID
            zoom = False

        img_rgb = self.get_rgb(local=local)
        centers = coords_from_points(self.centers, frame)
        com = coords_from_points(self.com, frame)

        tomato = {'centers': centers, 'radii': self.radii, 'com': com}
        plot_features(img_rgb, tomato=tomato, zoom=True)
        return figure_to_image(plt.gcf())

    def get_rgb(self, local=False):
        if local:
            return self.img_rgb_crop
        else:
            return self.img_rgb

    def get_truss_visualization(self, local=False, save=False, show_grasp_area=True):
        pwd = os.path.join(self.pwd, '08_result')

        if local:
            frame_id = self.LOCAL_FRAME_ID
            shape = self.shape  # self.bbox[2:4]
            zoom = True
            name = 'local'
            skeleton_width = 4
            grasp_linewidth = 3
        else:
            frame_id = self.ORIGINAL_FRAME_ID
            shape = self.shape
            zoom = False
            name = 'original'
            skeleton_width = 2
            grasp_linewidth = 1

        grasp, grasp_img, grasp_points = self.get_grasp_points(local=local, show_grasp_area=show_grasp_area)
        # tomato = self.get_tomatoes(local=local)
        xy_junc = coords_from_points(self.junction_points, frame_id)
        img = self.get_rgb(local=local)
        skeleton_img = self.get_skeleton_img(img.shape,local=local)

        # plot
        plt.figure()
        plot_image(img)
        # plot_features(tomato=tomato, zoom=zoom)
        visualize_skeleton(img, skeleton_img, coord_junc=xy_junc, show_img=False, skeleton_width=skeleton_width, 
                            show_grasp_area=show_grasp_area,grasp=grasp,grasp_img=grasp_img,grasp_points=grasp_points)
        
        if (grasp['bboxed_image']["xy"] is not None) and (grasp['bboxed_image']["angle"] is not None):
            settings = self.settings['compute_grasp']
            if self.px_per_mm is not None:
                minimum_grasp_length_px = self.px_per_mm * settings['grasp_length_min_mm']
                open_dist_px = settings['open_dist_mm'] * self.px_per_mm
                finger_thickenss_px = settings['finger_thinkness_mm'] * self.px_per_mm
            else:
                minimum_grasp_length_px = settings['grasp_length_min_px']
            plot_grasp_location(grasp['bboxed_image']["xy"], grasp['bboxed_image']["angle"], finger_width=minimum_grasp_length_px,
                                finger_thickness=finger_thickenss_px, finger_dist=open_dist_px, linewidth=grasp_linewidth)

        if save:
            if name is None:
                name = self.name
            else:
                name = self.name + '_' + name
            save_fig(plt.gcf(), pwd, name)

        return figure_to_image(plt.gcf())

    def get_segments(self, local=False):
        if local:
            tomato = self.tomato_crop # self.crop(self.tomato)
            peduncle = self.peduncle_crop # self.crop(self.peduncle)
            background = self.crop(self.background)
        else:
            tomato = self.tomato
            peduncle = self.peduncle
            background = self.background

        return tomato, peduncle, background

    def get_segmented_image(self, local=False):
        tomato, peduncle, background = self.get_segments(local=local)
        image_rgb = self.get_rgb(local=local)
        data = stack_segments(image_rgb, background, tomato, peduncle)
        return data

    def get_color_components(self):
        return self.img_hue

    def save_results(self, name, local=False, pwd=None):
        if pwd is None:
            pwd = self.pwd

        tomato, peduncle, background = self.get_segments(local=local)
        img_rgb = self.get_rgb(local=local)
        plot_segments(img_rgb, background, tomato, peduncle, linewidth=0.5, pwd=pwd, name=name, alpha=1)
    
    # def find_grasp_points(self, skeleton_img, local=False):

    #     if local:
    #         frame_id = self.LOCAL_FRAME_ID
    #     else:
    #         frame_id = self.ORIGINAL_FRAME_ID
        
    #     grasp_img = skeleton_img.copy()
    #     coord_junc, coord_end = get_node_coord(skeleton_img)
    #     all_coords = np.vstack((coord_junc,coord_end))

    #     for coord in all_coords:
    #         coord = coord.astype(int)

    #         radius = int(np.mean(grasp_img.shape) * 0.02)
    #         area_x = np.arange(coord[1]-radius,coord[1]+radius)
    #         area_y = np.arange(coord[0]-radius,coord[0]+radius)
    #         for i in range(len(area_x)):
    #             for j in range(len(area_y)):
    #                 grasp_img[area_x[i]][area_y[j]] = 0
        
    #     # find possible grasp points
    #     mask = []
    #     for i in range(len(grasp_img)):
    #         for j in range(len(grasp_img[i])):
    #             if grasp_img[i][j] == 1:
    #                 mask.append([i,j])
        
    #     subpath = [mask[0]]
    #     grasp_points = []
    #     angles = []
    #     for i in range(len(mask)-1):
    #         current_coord = mask[i]
    #         next_coord = mask[i+1]
    #         dist = np.sqrt((current_coord[0]-next_coord[0])**2 + (current_coord[1]-next_coord[1])**2)

    #         if dist < 10:
    #             subpath.append(next_coord)
    #         else:
    #             index = int(len(subpath)/2)
    #             grasp_point = subpath[index]
    #             grasp_point = [grasp_point[1], grasp_point[0]]
    #             grasp_points.append(grasp_point)

    #             start_node = subpath[0]
    #             end_node = subpath[-1]
    #             dx = end_node[1] - start_node[1]
    #             dy = end_node[0] - start_node[0]
    #             angle = np.arctan(dy/(dx+0.001))
    #             angles.append(angle)
                
    #             subpath = [next_coord]
        
    #     # when true -> closest to COM, else closest to peduncle mid_point
    #     close_to_COM = False
        
    #     if close_to_COM:
    #         mid_point = self.com.get_coord(frame_id)
    #     else:
    #         mid_point = self.peduncle_mid_point.get_coord(frame_id)

    #     shortest_dist = np.Inf
    #     for i in range(len(grasp_points)):
    #         grasp_point = grasp_points[i]
    #         dist = np.sqrt((mid_point[0]-grasp_point[0])**2 + (mid_point[1]-grasp_point[1])**2)

    #         if dist < shortest_dist:
    #             shortest_dist = dist
    #             i_shortest = i
        
    #     grasp_pixel = np.around(grasp_points[i_shortest]).astype(int)
    #     row = grasp_pixel[1]
    #     col = grasp_pixel[0]
    #     angle = angles[i_shortest]

    #     grasp_location = {"xy": grasp_pixel, "row": int(row), "col": int(col), "angle": angle}

    #     return grasp_location, grasp_img, grasp_points 

    @Timer("process image")
    def process_image(self):
        """
        Apply entire image processing pipeline
        returns: True if success, False if failed
        """

        self.color_space()

        success = self.segment_image(save_segment=False)
        if success is False:
            print("Failed to segment image")
            return success

        success = self.filter_image()
        if success is False:
            print ("Failed to filter image")
            return success

        success = self.rotate_cut_img()
        if success is False:
            print ("Failed to crop image")
            return success

        success = self.detect_peduncle()
        if success is False:
            print ("Failed to detect peduncle")
            return success

        # success = self.detect_tomatoes()
        # if success is False:
        #     print ("Failed to detect tomatoes")
        #     return success

        success = self.detect_grasp_location()
        if success is False:
            print ("Failed to detect grasp location")
        return success

    def get_settings(self):
        return self.settings

    def set_settings(self, settings):
        """
        Overwrites the settings which are present in the given dict
        """

        for key_1 in settings:
            for key_2 in settings[key_1]:
                self.settings[key_1][key_2] = settings[key_1][key_2]

    @Timer("bounding_box_detection")
    def bounding_box_detection(self, rgb_data=None, tomato_size=None, pwd_model=None, com_grasp=True):
        """
        Bounding box detection

        Output: saved bboxed images
        """
        inference_model = get_detection_model(pwd_model=pwd_model)

        num_detections, bboxes_pred = predict_truss(rgb_data, 
                                                    inference_model)
        if not com_grasp:
            bboxes_pred = widen_bboxes(bboxes=bboxes_pred)

        cropped_images, bboxes = create_bboxed_images(rgb_data, 
                                                      bboxes_pred, 
                                                      desired_size=510)

        return cropped_images, bboxes

def generate_tomato_info(pwd_root=None, file_name=None, tomato_size=None, bboxes=None, full_size_image_shape=None):
    """
    Generates tomato_info json files for bboxed images

    Output: json file with:
     - px_per_mm
     - tomato_size
     - bbox coordinates
     - full_size_image_shape
    """

    path = Path(os.getcwd())
    pwd_root = os.path.join(path.parent.parent, "doc/realsense_images/")

    pwd_depth_images = os.path.join(pwd_root, "data/depth_images/")
    file_name = file_name[:-4] + '.csv'
    pwd_depth_image = os.path.join(pwd_depth_images, file_name)
    
    df = pd.read_csv(pwd_depth_image, delimiter=';')

    # remove index column if present
    if df.shape[1] % 2 != 0:
        df.drop(df.columns[0], axis=1, inplace=True)
    
    array = pd.DataFrame.to_numpy(df, dtype=int)    
    avg_depth = find_mean_of_array(array)
    px_per_mm = find_px_per_mm(avg_depth, array.shape)
    
    for i in range(len(bboxes)):
        json_data = {'px_per_mm': px_per_mm,
                    'tomato_size': tomato_size,
                    'bbox': bboxes[i],
                    'full_size_image_shape': full_size_image_shape}
        pwd_json_file = os.path.join(pwd_root, 'data/json/' + file_name[:-4] + '_' + str(i) + '.json')
        with open(pwd_json_file, "w") as write_file:
            json.dump(json_data, write_file)

def load_tomato_info(pwd, img_id):
    pwd_info = os.path.join(pwd, img_id[:-4] + '.json')

    if not os.path.exists(pwd_info):
        print('Info does not exist for image: ' + img_id + ' continuing without info')
        return None

    with open(pwd_info, "r") as read_file:
        data_inf = json.load(read_file)

        tomato_info = {'px_per_mm': data_inf['px_per_mm']}

        if 'bbox' in data_inf.keys():
            tomato_info['bbox'] = data_inf['bbox']

        if 'tomato_size' in data_inf.keys():
            tomato_info['tomato_size'] = data_inf['tomato_size']
        else:
            tomato_info['tomato_size'] = 'big'

    return tomato_info

def load_depth(pwd_root=None, file_name=None):
    '''Load depth data if it's available'''
    
    try:
        pwd_depth_images = os.path.join(pwd_root, "data/depth_images/")
        file_name = file_name[:-6] + '.csv'
        pwd_depth_image = os.path.join(pwd_depth_images, file_name)
        
        df = pd.read_csv(pwd_depth_image, delimiter=';')

        # remove index column if present
        if df.shape[1] % 2 != 0:
            df.drop(df.columns[0], axis=1, inplace=True)
        
        depth_data = pd.DataFrame.to_numpy(df, dtype=int)

    except:
        print(f"No depth data available for image: {file_name}")
        depth_data=None
    
    return depth_data

def main():
    DIRECTORY = 'realsense_images/'
    # DIRECTORY = 'cropped_images/'
    save = False
    com_grasp = True
    bbox_detection = False
    tomato_size = 'small'
    drive = "backup"  # "UBUNTU 16_0"  #

    path = Path(os.getcwd())
    pwd_root = os.path.join(path.parent.parent, f"doc/{DIRECTORY}")
    pwd_images = os.path.join(pwd_root, "data/bboxed_images")
    pwd_json_read = os.path.join(pwd_root, "data/json")
    pwd_results = os.path.join(pwd_root, "results/")
    pwd_json_dump = os.path.join(pwd_results, 'json/')

    make_dirs(pwd_results)
    make_dirs(pwd_json_dump)

    process_image = ProcessImage(use_truss=True,
                                 pwd=pwd_results,
                                 save=save,
                                 com_grasp=com_grasp)

    if bbox_detection:
        pwd_full_size_images = os.path.join(pwd_root, "data/images/")
        pwd_detections = os.path.join(pwd_root, "results/bbox_detection/")

        model_dir = "bbox_detection/retinanet_465_imgs/"
        pwd_model = os.path.join(pwd_root, model_dir)

        images_list = os.listdir(pwd_full_size_images)
        images = [i for indx,i in enumerate(images_list) if images_list[indx][-4:] == '.png']
        
        for count, file_name in enumerate(images):
            print(f"Predicting trusses in image '{file_name}' ({count+1}/{len(images)})")
            
            rgb_data = load_rgb(file_name, pwd=pwd_full_size_images, horizontal=True)
            cropped_images, bboxes = process_image.bounding_box_detection(rgb_data=rgb_data, 
                                                                          tomato_size=tomato_size,
                                                                          pwd_model=pwd_model)

            for i in range(len(cropped_images)):
                cropped_images[i].save(pwd_detections + file_name[:-4] + '_' + str(i) + '.png')
            
            generate_tomato_info(pwd_root=pwd_root, 
                            file_name=file_name, 
                            tomato_size=tomato_size, 
                            bboxes=bboxes, 
                            full_size_image_shape=rgb_data.shape[:2])
    
    data = os.listdir(pwd_images)
    images = [i for indx,i in enumerate(data) if data[indx][-4:] == '.png']
    
    # select part of image set
    images = images[0:1]

    i_start = 1
    i_end = len(images) + 1
    N = len(images)

    for count, i_tomato in enumerate(images):
        print(f"Analyzing image '{i_tomato}' ({count+1}/{N})")

        tomato_name = i_tomato
        file_name = i_tomato

        rgb_data = load_rgb(file_name, pwd=pwd_images, horizontal=True, compress=False)
        tomato_info = load_tomato_info(pwd_json_read, tomato_name)
        depth_data = load_depth(pwd_root=pwd_root, file_name=file_name)
        process_image.add_image(rgb_data, tomato_info=tomato_info, name=tomato_name, depth_data=depth_data)

        success = process_image.process_image()
        process_image.get_truss_visualization(local=True, save=True, show_grasp_area=True)
        process_image.get_truss_visualization(local=False, save=True, show_grasp_area=False)

        json_data = process_image.get_object_features(tomato_info=tomato_info)

        pwd_json_file = os.path.join(pwd_json_dump, tomato_name + '.json')
        with open(pwd_json_file, "w") as write_file:
            json.dump(json_data, write_file)
        
        plt.close()

    if save is True:  # save is not True:
        plot_timer(Timer.timers['main'].copy(), threshold=0.02, pwd=pwd_results, name='main', title='Processing time',
                   startangle=-20)

    total_key = "process image"
    time_tot_mean = np.mean(Timer.timers[total_key]) / 1000
    time_tot_std = np.std(Timer.timers[total_key]) / 1000

    time_ms = Timer.timers[total_key]
    time_s = [x / 1000 for x in time_ms]

    time_min = min(time_s)
    time_max = max(time_s)

    print('Processing time: {mean:.2f}s +- {std:.2f}s (n = {n:d})'.format(mean=time_tot_mean, std=time_tot_std, n=N))
    print('Processing time lies between {time_min:.2f}s and {time_max:.2f}s (n = {n:d})'.format(time_min=time_min,
                                                                                                time_max=time_max, n=N))

    width = 0.5
    fig, ax = plt.subplots()

    ax.p1 = plt.bar(np.arange(i_start, i_end), time_s, width)

    plt.ylabel('time [s]')
    plt.xlabel('image ID')
    plt.title('Processing time per image')
    plt.rcParams["savefig.format"] = 'pdf'

    fig.show()
    fig.savefig(os.path.join(pwd_results, 'time_bar'), dpi=300)  # , bbox_inches='tight', pad_inches=0)


if __name__ == '__main__':
    main()
